"""The credential store's PostgreSQL posture: the fleet may not read secrets.

WHY THIS FILE EXISTS - AND SEPARATELY FROM `test_credential_store.py`

That file uses SQLite, which has no RLS, no roles and no `has_table_privilege`. Every property asserted
here is a PostgreSQL property, so a SQLite test cannot express it and would pass whatever the deployed
posture was.

The specific defect this guards against was found by verifying the DEPLOYED database rather than by a
test:

    AS granada_fleet (member of granada_app, NOBYPASSRLS):
      unbound             -> visible = 0
      bound to that org   -> visible = 1     <-- read credential ciphertext

`sql/fleet_role.sql` grants `granada_app` TO `granada_fleet`, so the fleet INHERITS every privilege the
application role has - including ones it was explicitly not granted. And the dispatcher's whole job is
to bind to each organisation in turn, so "bound to that org" is every organisation, one at a time.

That is the same reach as BYPASSRLS by a longer route, and it would have quietly undone ADR-0011.

SKIPPED WITHOUT A LIVE DATABASE, deliberately rather than silently: these are the only tests that can
see the difference, so a run that skips them should be read as "the posture is unverified", not as
"the posture is fine".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

def _postgres_url(*names: str) -> str:
    """Read a PostgreSQL URL from the environment, then from ``.env``.

    The same helper as `test_tenant_rls.py`, and for the same reason: `config.settings` cannot be used
    here, because conftest points `DATABASE_URL` at a SQLite scratch file before any test module imports
    `config`. Requiring the `postgresql` prefix is what makes the skip honest - a SQLite URL yields ""
    and the module skips rather than asserting against a database with no roles at all.
    """
    from dotenv import dotenv_values

    values = dotenv_values(BACKEND / ".env")
    for name in names:
        for candidate in (os.environ.get(name), values.get(name)):
            if candidate and candidate.startswith("postgresql"):
                return candidate
    return ""


ADMIN_URL = _postgres_url("GRANADA_ADMIN_DATABASE_URL", "DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="no PostgreSQL admin URL; the RLS and role posture cannot be asserted against SQLite",
)

TABLE = "credential_secrets"


@pytest.fixture(scope="module")
def conn():
    """A connection to the real database, or a SKIP if it cannot be reached.

    A declared URL is not a running server. This project's `.env` carries a `postgresql://localhost`
    URL even on a machine with no PostgreSQL, so `skipif(not ADMIN_URL)` alone let the module through
    and then errored six times in `setup` - which reads as "the code is broken" rather than "the
    posture is unverified here". The distinction matters: these are the only tests that can see the
    difference, so a skip must be legible.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(ADMIN_URL, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure means the same thing here
        engine.dispose()
        pytest.skip(f"PostgreSQL at the configured URL is not reachable: {type(exc).__name__}")
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def _fleet_is_member_of_app(conn) -> bool:
    from sqlalchemy import text

    return bool(
        conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_auth_members m
                      JOIN pg_roles r ON r.oid = m.member
                      JOIN pg_roles g ON g.oid = m.roleid
                     WHERE r.rolname = 'granada_fleet' AND g.rolname = 'granada_app'
                )
                """
            )
        ).scalar()
    )


def test_the_table_exists(conn):
    from sqlalchemy import text

    assert conn.execute(
        text("SELECT count(*) FROM information_schema.tables WHERE table_name = :t"),
        {"t": TABLE},
    ).scalar() == 1


def test_rls_is_enabled_and_forced(conn):
    """FORCE binds the table owner, so a migration or a maintenance script cannot quietly read every
    organisation's ciphertext."""
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
        {"t": TABLE},
    ).first()
    assert row is not None
    assert row[0] is True, "RLS is not enabled"
    assert row[1] is True, "RLS is not FORCED; the owner could read across tenants"


def test_a_restrictive_fleet_denial_policy_exists(conn):
    """A RESTRICTIVE policy ANDs with every permissive one, so `false` denies the fleet under ANY binding
    - which is what a REVOKE cannot do when the privilege arrives through role membership."""
    from sqlalchemy import text

    count = conn.execute(
        text(
            "SELECT count(*) FROM pg_policies "
            "WHERE tablename = :t AND permissive = 'RESTRICTIVE'"
        ),
        {"t": TABLE},
    ).scalar()
    assert count and count >= 1, (
        "no restrictive policy on credential_secrets; the fleet inherits SELECT through its "
        "membership in granada_app and can read credentials one organisation at a time"
    )


def test_the_fleet_cannot_read_a_credential_bound_to_the_right_organisation(conn):
    """THE test. `has_table_privilege` returns TRUE for the fleet because privilege is INHERITED - the
    thing that matters is whether a row actually comes back, and it must not."""
    from sqlalchemy import text

    if not _fleet_is_member_of_app(conn):
        pytest.skip("granada_fleet is not a member of granada_app in this deployment")

    real_org = conn.execute(
        text("SELECT org_id FROM agent_workflows LIMIT 1")
    ).scalar()
    if real_org is None:
        pytest.skip("no organisation with workflows to bind to")

    conn.execute(
        text(
            f"INSERT INTO {TABLE} (id, org_id, ref, kind, ciphertext, status, created_at) "
            "VALUES ('posture-probe', :org, 'posture-probe', 'API_KEY', 'probe', 'ACTIVE', now()) "
            "ON CONFLICT (id) DO UPDATE SET ciphertext = 'probe'"
        ),
        {"org": real_org},
    )
    try:
        # SEPARATE execute() CALLS. SQLAlchemy 2.0 refuses more than one statement per execute, and a
        # `SET ROLE` sent in the same string as the SELECT is parsed as a syntax error rather than
        # performed - which would make this test pass by never entering the fleet role at all.
        conn.execute(text("SET ROLE granada_fleet"))
        conn.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"), {"org": real_org}
        )
        visible = conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
        assert visible == 0, (
            "granada_fleet read a credential while bound to the owning organisation. It inherits "
            "SELECT through granada_app, and binding per organisation is exactly what the dispatcher "
            "does for every tenant."
        )
    finally:
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DELETE FROM {TABLE} WHERE id = 'posture-probe'"))


def test_the_application_role_can_still_read_its_own_credential(conn):
    """The fleet denial must not have denied the application too - a fix that closes the hole by
    breaking the feature is not a fix."""
    from sqlalchemy import text

    visible = conn.execute(
        text(f"SELECT has_table_privilege('granada_app', '{TABLE}', 'SELECT')")
    ).scalar()
    assert visible is True, "granada_app cannot read credentials; the store is unusable"


def test_delete_is_revoked_and_update_is_not(conn):
    """THE TENTH ADDITIVE-GRANT TRAP.

    `sql/grant_runtime_role.sql` begins with
    `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO granada_app` and runs AFTER
    migrations, so a migration's REVOKE does not survive it. DELETE must be re-revoked in the grant
    script's list; UPDATE must stay, because rotating a credential is an UPDATE.
    """
    from sqlalchemy import text

    can_delete = conn.execute(
        text(f"SELECT has_table_privilege('granada_app', '{TABLE}', 'DELETE')")
    ).scalar()
    can_update = conn.execute(
        text(f"SELECT has_table_privilege('granada_app', '{TABLE}', 'UPDATE')")
    ).scalar()

    assert can_delete is False, (
        "granada_app can DELETE credentials. A credential is revoked by destroying its ciphertext, not "
        "by deleting the row - the row is what answers 'when did this stop working'."
    )
    assert can_update is True, (
        "granada_app cannot UPDATE credentials, so a credential could never be rotated"
    )
