"""Prove that row-level security actually denies cross-tenant access.

These tests are the evidence for the Phase 1 requirement that tenant
isolation is enforced at the data tier. Application-level checks are not
sufficient evidence, because a single missing ``WHERE`` clause anywhere in the
service layer would bypass them entirely; these tests bypass the service layer
completely and go straight at the database.

Why these tests skip on SQLite
-----------------------------
SQLite has no row-level security. A test that silently passed on SQLite while
proving nothing would be worse than no test at all, so the whole module skips
unless the target is PostgreSQL. ``test_module_requires_postgresql`` asserts
that the skip is genuine rather than an accidental pass.

Why a scratch schema instead of a scratch database
--------------------------------------------------
Creating a database needs CREATEDB or superuser, which this role deliberately
does not have - least privilege is the point. Instead the migrations run with
``search_path`` pointed at a throwaway schema, so the real ``granada_auth``
schema is never written to even by accident.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tenant_context import (  # noqa: E402
    apply_tenant_on_checkout,
    clear_tenant,
    set_tenant,
    tenant_scope,
    unscoped,
)


def _postgres_url(*names: str) -> str:
    """Read a PostgreSQL URL from the environment, then from ``.env``.

    ``config.settings`` cannot be used here: conftest sets ``DATABASE_URL`` to
    a SQLite scratch file before any test module imports ``config``, so the
    settings singleton would report the scratch database rather than the real
    one.

    The two URLs are deliberately different roles:

    ``GRANADA_ADMIN_DATABASE_URL``  the schema owner. Needs CREATE on the
                                   database and will own every table it builds.
    ``GRANADA_RUNTIME_DATABASE_URL`` the least-privilege role the application
                                   actually connects as.

    Falling back to ``.env`` keeps the module usable for a developer who has only
    ever had one local role; anything that is not PostgreSQL yields ``""`` and
    the module skips.
    """
    from dotenv import dotenv_values

    values = dotenv_values(BACKEND / ".env")
    for name in names:
        for candidate in (os.environ.get(name), values.get(name)):
            if candidate and candidate.startswith("postgresql"):
                return candidate
    return ""


# Owner / migrating role. Without it there is nowhere to put the scratch schema.
ADMIN_URL = _postgres_url("GRANADA_ADMIN_DATABASE_URL", "DATABASE_URL")

# The role the application connects as. Optional: without it the ``org_members``
# tests, which can only be proven for a non-owner, skip loudly.
RUNTIME_URL = _postgres_url("GRANADA_RUNTIME_DATABASE_URL", "DATABASE_URL")


def _runtime_role_name() -> str:
    """Extract the role name from the runtime URL.

    Used so the GRANT statements name whatever role the developer configured
    rather than assuming ``granada_app`` - a CI box may use a different name,
    and a hardcoded GRANT that silently does nothing is worse than no grant.
    """
    from urllib.parse import urlparse

    parsed = urlparse(RUNTIME_URL)
    return parsed.username or "granada_app"


PG_URL = ADMIN_URL


pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "RLS enforcement requires a real PostgreSQL target; SQLite has no RLS. "
        "Set GRANADA_ADMIN_DATABASE_URL to the schema-owner credentials."
    ),
)


def _real_alembic():
    """Import the *installed* alembic, not the local ``alembic/`` package.

    ``Auth/backend/alembic/`` contains an ``__init__.py``, so it is a real
    package whose name shadows the installed alembic distribution whenever the
    backend directory leads ``sys.path`` (recorded as ADR-0003; the directory
    was kept rather than renamed because ``alembic.ini`` points at it and
    renaming churns every migration path).

    ``conftest`` performs the de-shadowing once for the whole process, because
    doing it per-module made the result depend on collection order - see the
    note on ``conftest.import_real_alembic``. This wrapper keeps the module
    self-documenting and gives both callers one implementation.
    """
    from conftest import ALEMBIC_COMMAND, ALEMBIC_CONFIG

    return ALEMBIC_COMMAND, ALEMBIC_CONFIG


@pytest.fixture(scope="module")
def pg_engine():
    """Build a migrated scratch schema on the real PostgreSQL server.

    Returns a namespace with two engines:

    ``owner``   - the migrating role, which owns every table.
    ``runtime`` - the least-privilege application role, or ``None``.

    Both are needed, and the reason is the ``org_members`` trade-off recorded
    in ADR-0005. That table is ENABLE but not FORCE row-level security,
    because the SECURITY DEFINER bootstrap helper that resolves which tenant
    a user belongs to must run as the owner. The cost is that the owner is not
    bound by ``org_members`` policies - so ``org_members`` isolation is only
    provable as the runtime role. Every other tenant table is FORCE, so it is
    proven even for the owner.
    """
    from types import SimpleNamespace

    command, Config = _real_alembic()

    schema = f"rls_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()

    # search_path is set through the connection string so that Alembic's own
    # engine - which this module does not control - writes into the scratch
    # schema. Every unqualified CREATE TABLE therefore lands there.
    scoped = f"{PG_URL}?options=-csearch_path%3D{schema}"

    previous = os.environ.get("DATABASE_URL")
    previous_admin = os.environ.get("GRANADA_ADMIN_DATABASE_URL")
    os.environ["DATABASE_URL"] = scoped
    # env.py now prefers GRANADA_ADMIN_DATABASE_URL, and it takes precedence in
    # the process environment, so pinning only DATABASE_URL would let the run
    # migrate the real public schema. Both are pinned, and both are restored.
    os.environ["GRANADA_ADMIN_DATABASE_URL"] = scoped
    try:
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))

        # Seed reference data BEFORE row-level security is switched on.
        #
        # FORCE ROW LEVEL SECURITY binds the table owner too, so once
        # revision 003 lands there is deliberately no way for any role - not
        # even the owner - to insert a shared system row such as a role with
        # org_id IS NULL. That is the intended price of enforcement: a tenant
        # must not be able to mint a globally visible role. It also means
        # production reference data has to be seeded before the upgrade, which
        # is the sequence exercised here.
        command.upgrade(cfg, "002_phase1_schema_alignment")
        engine = create_engine(scoped)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO roles (id, org_id, key, name, is_system) "
                        "VALUES (:i, NULL, 'platform_admin', 'Platform Admin', true)"
                    ),
                    {"i": str(uuid.uuid4())},
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        if previous_admin is None:
            os.environ.pop("GRANADA_ADMIN_DATABASE_URL", None)
        else:
            os.environ["GRANADA_ADMIN_DATABASE_URL"] = previous_admin

    runtime_url = RUNTIME_URL
    runtime = None
    if runtime_url:
        role = _runtime_role_name()
        # The runtime role is not the owner, so GRANT - never FORCE - is what
        # lets it read and write here. Ownership stays with the migrating role,
        # which is the whole point of the split.
        grant = create_engine(scoped, isolation_level="AUTOCOMMIT")
        try:
            with grant.connect() as conn:
                conn.execute(text(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO "{role}"'))
                conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'))
                conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"'))
                # Deliberately unqualified: this engine's search_path is already
                # the scratch schema (set through the connection options), and
                # the function name contains a dot that PostgreSQL would parse
                # as schema.app rather than schema."app".
                conn.execute(
                    text(f'GRANT EXECUTE ON FUNCTION app.user_org_ids(text) TO "{role}"')
                )
        finally:
            grant.dispose()
        runtime = create_engine(f"{runtime_url}?options=-csearch_path%3D{schema}")

    owner = create_engine(scoped)
    try:
        yield SimpleNamespace(owner=owner, runtime=runtime, schema=schema)
    finally:
        owner.dispose()
        if runtime is not None:
            runtime.dispose()
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _require_runtime(pg_engine):
    """Return the runtime engine, or skip loudly if it was not configured.

    Skipping here is visible and names the variable to set; it is never a
    silent pass. ``org_members`` isolation cannot be proven any other way,
    because that table is not FORCE (ADR-0005), so this skip must not be
    mistaken for a green result.
    """
    if pg_engine.runtime is None:
        pytest.skip(
            "org_members row-level isolation is only provable as the non-owner "
            "runtime role. Set GRANADA_RUNTIME_DATABASE_URL to a "
            "postgresql://granada_app:... URL to run these."
        )
    return pg_engine.runtime


def _make_user(conn, name: str) -> str:
    uid = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO users (id, display_name, locale, status, created_at) "
            "VALUES (:id, :name, 'en', 'active', now())"
        ),
        {"id": uid, "name": name},
    )
    return uid


def _make_user_as_owner(pg_engine, name: str) -> str:
    """``users`` carries no RLS, so the owner can create invitees directly."""
    with pg_engine.owner.begin() as conn:
        return _make_user(conn, name)


@pytest.fixture(scope="module")
def tenants(pg_engine):
    """Create two unrelated tenants, each with an owner and a founder role."""
    from tenant_context import create_tenant_and_membership

    engine = pg_engine.owner
    made = {}

    # Users are committed in their own transaction first.
    # create_tenant_and_membership opens a second transaction on its own
    # connection, so a user still uncommitted in this one would be invisible to
    # it and the organisations.owner_user_id foreign key would fail. This
    # mirrors registration, where the user exists before the tenant does.
    with engine.begin() as conn:
        owners = {key: _make_user(conn, f"{key}-owner") for key in ("alpha", "beta")}

    for key, owner in owners.items():
        role_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        create_tenant_and_membership(
            engine,
            org_id=org_id,
            user_id=owner,
            org_name=f"{key}-org",
            org_slug=f"{key}-org",
            role_id=role_id,
            role_key="owner",
            role_name="Owner",
        )
        made[key] = {"org_id": org_id, "user_id": owner, "role_id": role_id}
    return made


# ----------------------------------------------------------------------
# The deny-by-default property
# ----------------------------------------------------------------------

def test_unknown_tenant_reads_nothing(pg_engine):
    """No tenant context must mean no rows - never a default tenant.

    Only the FORCE-bound tables are asserted here. ``org_members`` is
    deliberately not FORCE (see ADR-0005), so the owner still sees its rows
    here; its unscoped behaviour is proven as the runtime role in
    ``test_unscoped_runtime_sees_no_memberships``.
    """
    engine = pg_engine.owner
    with unscoped(engine) as conn:
        for table in ("organisations", "saml_providers", "audit_logs"):
            seen = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert seen == 0, f"unscoped request saw {seen} rows in {table}"

        # `roles` is excluded from the zero check on purpose: a role with
        # org_id IS NULL is a shared SYSTEM role that every tenant is meant to
        # be able to read, so one row is correct here. What must be absent is
        # any *tenant* role.
        tenant_roles = conn.execute(
            text("SELECT count(*) FROM roles WHERE org_id IS NOT NULL")
        ).scalar()
        assert tenant_roles == 0, f"unscoped request saw {tenant_roles} tenant roles"


def test_empty_tenant_string_reads_nothing(pg_engine):
    """'' must collapse to NULL, so it cannot be used to widen access."""
    engine = pg_engine.owner
    with tenant_scope(engine, org_id="") as conn:
        assert conn.execute(
            text("SELECT count(*) FROM organisations")
        ).scalar() == 0


def test_tenant_reads_only_its_own_rows(pg_engine, tenants):
    engine = pg_engine.owner
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        names = [
            r[0]
            for r in conn.execute(text("SELECT name FROM organisations")).all()
        ]
    assert names == ["alpha-org"], f"tenant alpha saw {names}"


# ----------------------------------------------------------------------
# Cross-tenant reads
# ----------------------------------------------------------------------

def test_cannot_read_another_tenants_organisation(pg_engine, tenants):
    engine = pg_engine.owner
    beta_org = tenants["beta"]["org_id"]
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        found = conn.execute(
            text("SELECT id FROM organisations WHERE id = :i"), {"i": beta_org}
        ).all()
    assert found == [], "tenant alpha read tenant beta's organisation row"


def test_cannot_read_another_tenants_members(pg_engine, tenants):
    """org_members isolation, proven as the runtime role.

    This is the one tenant table whose policies cannot be demonstrated as the
    owner, because it is not FORCE and the owner bypasses ENABLE.
    """
    runtime = _require_runtime(pg_engine)
    with tenant_scope(runtime, org_id=tenants["alpha"]["org_id"]) as conn:
        users = [
            r[0] for r in conn.execute(text("SELECT user_id FROM org_members")).all()
        ]
    assert users == [tenants["alpha"]["user_id"]], f"leaked memberships: {users}"


def test_unscoped_runtime_sees_no_memberships(pg_engine, tenants):
    """Deny-by-default must hold for the role the application actually uses."""
    runtime = _require_runtime(pg_engine)
    with unscoped(runtime) as conn:
        seen = conn.execute(text("SELECT count(*) FROM org_members")).scalar()
    assert seen == 0, "the runtime role saw memberships with no tenant set"


def test_cannot_read_another_tenants_roles(pg_engine, tenants):
    """A tenant sees its own role plus shared system roles - never beta's.

    Asserting on ids rather than keys matters here: both tenants name their
    founder role "owner", so a key-based assertion would pass even if beta's
    row leaked in and one of alpha's were hidden.
    """
    engine = pg_engine.owner
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        visible = {r[0] for r in conn.execute(text("SELECT id FROM roles")).all()}

    assert tenants["alpha"]["role_id"] in visible, "tenant lost its own role"
    assert tenants["beta"]["role_id"] not in visible, "tenant alpha read tenant beta's role"


# ----------------------------------------------------------------------
# Cross-tenant writes
# ----------------------------------------------------------------------

def test_cannot_add_a_member_to_another_tenant(pg_engine, tenants):
    runtime = _require_runtime(pg_engine)
    intruder = tenants["alpha"]["user_id"]
    with pytest.raises(Exception) as exc:
        with tenant_scope(runtime, org_id=tenants["alpha"]["org_id"]) as conn:
            conn.execute(
                text(
                    "INSERT INTO org_members (org_id, user_id, role_id, joined_at) "
                    "VALUES (:o, :u, :r, now())"
                ),
                {
                    "o": tenants["beta"]["org_id"],
                    "u": intruder,
                    "r": tenants["beta"]["role_id"],
                },
            )
    assert "row-level security" in str(exc.value).lower()


def test_can_add_a_member_to_own_tenant(pg_engine, tenants):
    """The positive control: without this, a broken policy could pass."""
    runtime = _require_runtime(pg_engine)
    invitee = _make_user_as_owner(pg_engine, "invited-user")
    alpha = tenants["alpha"]
    with tenant_scope(runtime, org_id=alpha["org_id"]) as conn:
        conn.execute(
            text(
                "INSERT INTO org_members (org_id, user_id, role_id, joined_at) "
                "VALUES (:o, :u, :r, now())"
            ),
            {"o": alpha["org_id"], "u": invitee, "r": alpha["role_id"]},
        )
        assert conn.execute(text("SELECT count(*) FROM org_members")).scalar() == 2


def test_cannot_update_another_tenants_organisation(pg_engine, tenants):
    engine = pg_engine.owner
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        result = conn.execute(
            text("UPDATE organisations SET name = 'stolen' WHERE id = :i"),
            {"i": tenants["beta"]["org_id"]},
        )
        assert result.rowcount == 0, "update reached another tenant's row"


def test_cannot_delete_another_tenants_organisation(pg_engine, tenants):
    engine = pg_engine.owner
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        result = conn.execute(
            text("DELETE FROM organisations WHERE id = :i"),
            {"i": tenants["beta"]["org_id"]},
        )
        assert result.rowcount == 0, "delete reached another tenant's row"


def test_cannot_create_a_global_role(pg_engine):
    """A tenant must not be able to mint a system role visible to everyone."""
    engine = pg_engine.owner
    with pytest.raises(Exception) as exc:
        with tenant_scope(engine, org_id=None) as conn:
            conn.execute(
                text(
                    "INSERT INTO roles (id, org_id, key, name, is_system) "
                    "VALUES (:i, NULL, 'superuser', 'Superuser', true)"
                ),
                {"i": str(uuid.uuid4())},
            )
    assert "row-level security" in str(exc.value).lower()


# ----------------------------------------------------------------------
# System roles and audit logs
# ----------------------------------------------------------------------

def test_system_role_is_readable_but_not_writable(pg_engine):
    """Shared system roles stay visible to every tenant and editable by none.

    The ``platform_admin`` row was seeded by the fixture before revision 003
    applied; it is looked up rather than inserted, which is itself part of the
    proof: under FORCE RLS nothing can insert a global role afterwards.
    """
    engine = pg_engine.owner
    with engine.begin() as conn:
        role_id = conn.execute(
            text("SELECT id FROM roles WHERE org_id IS NULL AND key = 'platform_admin'")
        ).scalar_one()

    with tenant_scope(engine, org_id=None) as conn:
        keys = [r[0] for r in conn.execute(text("SELECT key FROM roles")).all()]
        assert "platform_admin" in keys, "system role must stay readable"

        result = conn.execute(
            text("UPDATE roles SET name = 'Owned' WHERE id = :i"), {"i": role_id}
        )
        assert result.rowcount == 0, "a tenant rewrote a system role"


def test_nothing_can_insert_a_system_role_after_enforcement(pg_engine):
    """The owner is bound too - that is what FORCE buys us."""
    engine = pg_engine.owner
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO roles (id, org_id, key, name, is_system) "
                    "VALUES (:i, NULL, 'backdoor', 'Backdoor', true)"
                ),
                {"i": str(uuid.uuid4())},
            )
    assert "row-level security" in str(exc.value).lower()


def test_audit_log_is_append_only(pg_engine, tenants):
    engine = pg_engine.owner
    alpha = tenants["alpha"]
    with tenant_scope(engine, org_id=alpha["org_id"], user_id=alpha["user_id"]) as conn:
        conn.execute(
            text(
                "INSERT INTO audit_logs (id, user_id, org_id, event, ip, created_at) "
                "VALUES (:i, :u, :o, 'test.event', '127.0.0.1', now())"
            ),
            {"i": str(uuid.uuid4()), "u": alpha["user_id"], "o": alpha["org_id"]},
        )
        read_back = conn.execute(
            text("SELECT count(*) FROM audit_logs WHERE event = 'test.event'")
        ).scalar()
        assert read_back == 1

        deleted = conn.execute(
            text("DELETE FROM audit_logs WHERE event = 'test.event'")
        )
        assert deleted.rowcount == 0, "a tenant deleted its own audit trail"

        updated = conn.execute(
            text("UPDATE audit_logs SET event = 'tampered' WHERE event = 'test.event'")
        )
        assert updated.rowcount == 0, "a tenant rewrote its own audit trail"


def test_audit_insert_allowed_without_tenant(pg_engine):
    """A failed login has no tenant; refusing to log it would be unacceptable."""
    engine = pg_engine.owner
    with unscoped(engine) as conn:
        conn.execute(
            text(
                "INSERT INTO audit_logs (id, user_id, org_id, event, ip, created_at) "
                "VALUES (:i, NULL, NULL, 'auth.login.failed', '10.0.0.1', now())"
            ),
            {"i": str(uuid.uuid4())},
        )


# ----------------------------------------------------------------------
# Connection-pool safety
# ----------------------------------------------------------------------

def test_tenant_does_not_leak_through_the_pool(pg_engine, tenants):
    """The setting is transaction-scoped, so the next borrower starts blank."""
    engine = pg_engine.owner
    with tenant_scope(engine, org_id=tenants["alpha"]["org_id"]) as conn:
        assert conn.execute(
            text("SELECT count(*) FROM organisations")
        ).scalar() == 1

    # Same engine, therefore potentially the same pooled connection.
    with unscoped(engine) as conn:
        assert conn.execute(
            text("SELECT count(*) FROM organisations")
        ).scalar() == 0, "tenant alpha leaked onto a later connection"


# ----------------------------------------------------------------------
# Session-scoped tenant binding, across commits and pool check-outs
# ----------------------------------------------------------------------

def _orm_session_factory(engine):
    """A real ORM session factory with the production check-out backstop attached.

    The production engine registers :func:`apply_tenant_on_checkout` on the
    ``Engine`` class, so every engine in the process inherits it. Registering it
    on this engine instance reproduces that faithfully without importing
    ``database``, which would pull the application's configured engine and URL
    into a module that is only about policies.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    event.listen(engine, "checkout", apply_tenant_on_checkout)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _org_count(session) -> int:
    return session.execute(text("SELECT count(*) FROM organisations")).scalar()


def test_session_tenant_survives_commit(pg_engine, tenants):
    """``Session.commit()`` must not silently drop the tenant mid-request.

    This is the most important behaviour in the module, and it was wrong before
    anything tested it. ``Session.commit()`` ends the transaction *and* returns
    the DBAPI connection to the pool. The next query in the same request
    therefore borrows a connection that the check-out listener has just
    blanked, so without ``_rebind_after_transaction`` the request runs with
    "tenant unknown" - which the policies deny.

    The failure mode is closed: it leaks nothing, it breaks legitimate traffic.
    Organisation creation returned HTTP 500 against real PostgreSQL while all
    119 SQLite tests stayed green, because SQLite has no policies to fail.
    """
    Session = _orm_session_factory(pg_engine.owner)
    session = Session()
    try:
        set_tenant(session, tenants["alpha"]["org_id"], tenants["alpha"]["user_id"])
        assert _org_count(session) == 1, "precondition: the tenant reads its own row"

        session.commit()  # <- the connection goes back to the pool here

        assert _org_count(session) == 1, (
            "the tenant was dropped by Session.commit(); the request is now "
            "running unscoped and reads nothing"
        )
    finally:
        clear_tenant(session)
        session.close()


def test_a_session_that_established_no_tenant_reads_nothing(pg_engine):
    """The complementary half: nothing inherits a tenant it did not establish itself.

    Deliberately depends on the previous test having run - same engine, same
    pool, same connections - because a check-out backstop that only looks
    correct on a freshly created pool proves nothing.
    """
    Session = _orm_session_factory(pg_engine.owner)
    session = Session()
    try:
        assert _org_count(session) == 0, "a tenant survived onto a pooled connection"
    finally:
        session.close()


def test_clear_tenant_denies_the_session_again(pg_engine, tenants):
    """Clearing is not advisory - it takes effect on the very next query."""
    Session = _orm_session_factory(pg_engine.owner)
    session = Session()
    try:
        set_tenant(session, tenants["alpha"]["org_id"], tenants["alpha"]["user_id"])
        assert _org_count(session) == 1, "precondition"
        clear_tenant(session)
        assert _org_count(session) == 0, "clear_tenant did not take effect"
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tenant resolution bootstrap
# ----------------------------------------------------------------------

def test_user_org_ids_resolves_the_tenant_before_it_is_known(pg_engine, tenants):
    """Without this helper, tenant resolution is impossible under RLS.

    Establishing which tenant a user belongs to is the first thing an
    authenticated request must do, and it cannot be done through a policy
    keyed on the very tenant being resolved. This proves the documented
    escape hatch works from an unscoped transaction.
    """
    engine = pg_engine.owner
    with unscoped(engine) as conn:
        assert conn.execute(
            text("SELECT count(*) FROM organisations")
        ).scalar() == 0, "precondition: still unscoped"

        resolved = [
            r[0]
            for r in conn.execute(
                text("SELECT app.user_org_ids(:u)"), {"u": tenants["beta"]["user_id"]}
            ).all()
        ]

    assert resolved == [tenants["beta"]["org_id"]]


def test_user_org_ids_returns_nothing_for_null(pg_engine):
    engine = pg_engine.owner
    with unscoped(engine) as conn:
        assert conn.execute(
            text("SELECT count(*) FROM app.user_org_ids(NULL)")
        ).scalar() == 0, "NULL must never be treated as a wildcard"


def test_user_org_ids_does_not_leak_other_tenants(pg_engine, tenants):
    engine = pg_engine.owner
    with unscoped(engine) as conn:
        resolved = [
            r[0]
            for r in conn.execute(
                text("SELECT app.user_org_ids(:u)"),
                {"u": tenants["alpha"]["user_id"]},
            ).all()
        ]
    assert resolved == [tenants["alpha"]["org_id"]], f"got {resolved}"


def test_module_requires_postgresql():
    """Guard against the skip becoming a silent pass."""
    assert PG_URL.startswith("postgresql"), (
        "RLS tests must run against PostgreSQL; SQLite cannot prove anything "
        "about row-level security."
    )


def test_runtime_role_cannot_delete_the_ledger_or_the_evidence(pg_engine):
    """Least privilege on the tables that record what the platform did.

    Migrations 004 and 005 grant only SELECT/INSERT/UPDATE on ``jobs``,
    ``job_attempts`` and ``model_invocations``: the application records its
    work, it does not erase it. Purging a ledger is an administrative act and
    belongs to the owner role.

    **This test builds its own schema on purpose.** ``pg_engine`` grants
    ``ALL ON ALL TABLES`` to the runtime role, and that is correct for what it
    is for: it makes a forbidden DELETE fail because of *row-level security*
    rather than because of a missing privilege, which is the property those 24
    tests are asserting. Privileges are therefore not observable there, and
    asserting them against that fixture would have measured the fixture rather
    than the migrations.

    Here the only table privileges are the ones the migrations themselves
    confer, which is the production posture. The schema USAGE grant is not a
    concession - without it the role cannot reach any object at all, and
    migrations deliberately do not hand out schema-level rights.

    ``has_table_privilege`` is used rather than
    ``information_schema.role_table_grants`` because the view is not
    authoritative: it has attributed privileges to ``granada_app`` that a live
    connection was refused. TRUNCATE is included because it bypasses row-level
    security entirely.
    """
    command, Config = _real_alembic()
    role = _runtime_role_name()
    schema = f"priv_test_{uuid.uuid4().hex[:10]}"

    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()

    scoped = f"{PG_URL}?options=-csearch_path%3D{schema}"
    previous = os.environ.get("DATABASE_URL")
    previous_admin = os.environ.get("GRANADA_ADMIN_DATABASE_URL")
    os.environ["DATABASE_URL"] = scoped
    os.environ["GRANADA_ADMIN_DATABASE_URL"] = scoped
    try:
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        command.upgrade(cfg, "head")

        grant = create_engine(scoped, isolation_level="AUTOCOMMIT")
        try:
            with grant.connect() as conn:
                # USAGE only. No table privileges: those must come from the
                # migrations' own _grant_runtime().
                conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        finally:
            grant.dispose()

        engine = create_engine(scoped)
        try:
            with engine.connect() as conn:
                for name, allowed in (
                    ("jobs", ("SELECT", "INSERT", "UPDATE")),
                    ("job_attempts", ("SELECT", "INSERT", "UPDATE")),
                    ("model_invocations", ("SELECT", "INSERT", "UPDATE")),
                    # The Digital Twin and the vault. Their history IS the
                    # product: superseding a fact is an UPDATE, and that only
                    # works if the old row survives. A runtime role able to
                    # DELETE could erase the version an application was
                    # submitted against - exactly what the "Why?" view needs.
                    ("org_facts", ("SELECT", "INSERT", "UPDATE")),
                    ("documents", ("SELECT", "INSERT", "UPDATE")),
                ):
                    for privilege in allowed:
                        assert conn.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": f'"{schema}".{name}', "p": privilege},
                        ).scalar(), (
                            f"{role} is missing {privilege} on {name}; migration "
                            "_grant_runtime() did not reach the scratch schema"
                        )

                    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                        assert not conn.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": f'"{schema}".{name}', "p": privilege},
                        ).scalar(), (
                            f"{role} holds {privilege} on {name}; the runtime must "
                            "not be able to erase or bypass the record of its own work"
                        )

                # The posture that motivated the whole privilege split.
                assert not conn.execute(
                    text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                    {"r": role, "t": f'"{schema}".alembic_version'},
                ).scalar(), f"{role} can read alembic_version"

                # -- the append-only audit trail --------------------------
                # ``application_transitions`` is history. A trail its own subject
                # can rewrite is worse than no trail, because it looks
                # authoritative. UPDATE is refused here as well as DELETE, and
                # that is asserted separately because it is the one table where
                # the difference matters.
                for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                    assert not conn.execute(
                        text("SELECT has_table_privilege(:r, :t, :p)"),
                        {"r": role, "t": f'"{schema}".application_transitions', "p": privilege},
                    ).scalar(), (
                        f"{role} holds {privilege} on application_transitions; an "
                        "append-only history must not be rewritable"
                    )
                # -- the append-only records ------------------------------
                # History and evidence. A trail its own subject can rewrite is
                # worse than no trail, because it looks authoritative. UPDATE is
                # refused here as well as DELETE, and that is asserted separately
                # because these are the tables where the difference matters.
                for append_only in (
                    "application_transitions", "agent_activity", "donor_research",
                ):
                    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                        assert not conn.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": f'"{schema}".{append_only}', "p": privilege},
                        ).scalar(), (
                            f"{role} holds {privilege} on {append_only}; an "
                            "append-only record must not be rewritable"
                        )
                    for privilege in ("SELECT", "INSERT"):
                        assert conn.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": f'"{schema}".{append_only}', "p": privilege},
                        ).scalar(), f"{role} cannot {privilege} {append_only}"
        finally:
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        if previous_admin is None:
            os.environ.pop("GRANADA_ADMIN_DATABASE_URL", None)
        else:
            os.environ["GRANADA_ADMIN_DATABASE_URL"] = previous_admin

        cleanup = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with cleanup.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            cleanup.dispose()