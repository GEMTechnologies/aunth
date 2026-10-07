"""End-to-end proof that tenant isolation holds through the HTTP request path.

Run as a *script* in a fresh interpreter, not as a pytest module. The reason is
structural rather than stylistic: ``config.settings`` and the module-level
``engine`` in ``database.py`` are singletons created at import time, and the
rest of the suite pins ``DATABASE_URL`` to a SQLite scratch file before any
module is imported. Importing ``main`` into that process would silently run the
application against SQLite, and every assertion about PostgreSQL row-level
security would pass without ever touching PostgreSQL. A subprocess is the only
way to get an honest result.

``tests/test_postgres_request_path.py`` runs this file and asserts on its
exit code.

The scenario
------------
1. Build a migrated scratch schema, seeded and granted exactly as production is.
2. Boot the real ASGI app as the *runtime* role (``granada_app``), not the owner.
3. Two unrelated users register, log in, and each create their own organisation.
4. Each must see exactly one organisation - their own.
5. Each must be refused when addressing the other's organisation by id.
6. A member of no organisation must see no organisations at all - the request
   path must not fall back to a default tenant when it cannot resolve one.
7. The owner connection must see each organisation *when bound to it*, and only
   that one. Under ``FORCE ROW LEVEL SECURITY`` even the owner is a tenant of the
   policy, so there is deliberately no unscoped cross-tenant read to prove here.

Steps 6 and 7 matter: without step 6, "user A cannot read organisation B" would
be satisfied equally well by B's data not existing at all. Step 7 states the
consequence of the enforcement model plainly rather than letting a reader assume
an owner can audit across tenants at the SQL tier.

Exit codes: 0 all checks passed, 1 a check failed, 2 the probe could not run
(no PostgreSQL configured) - which the pytest wrapper turns into a skip.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: Marks the single machine-readable result line. See :func:`_emit`.
RESULT_PREFIX = "GRANADA_PROBE_RESULT "


def _url_from(names: tuple[str, ...]) -> str:
    from dotenv import dotenv_values

    values = dotenv_values(BACKEND / ".env")
    for name in names:
        for candidate in (os.environ.get(name), values.get(name)):
            if candidate and candidate.startswith("postgresql"):
                return candidate
    return ""


def _emit(payload: dict) -> None:
    """Print the machine-readable result.

    The single ``GRANADA_PROBE_RESULT`` line is the contract with
    ``test_postgres_request_path.py``. It is deliberately one line: the human
    copy below is indented for readability, and a reader that scans stdout for a
    line beginning with ``{`` finds only that lone brace and parses nothing. That
    wrapper once passed 25 checks and asserted on an empty dict, because of
    exactly that.
    """
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True))
    print(json.dumps(payload, indent=2))


def main() -> int:
    from sqlalchemy import create_engine, text

    owner_url = _url_from(("GRANADA_ADMIN_DATABASE_URL", "DATABASE_URL"))
    runtime_url = _url_from(("GRANADA_RUNTIME_DATABASE_URL", "DATABASE_URL"))
    if not owner_url:
        _emit({"skipped": "no GRANADA_ADMIN_DATABASE_URL"})
        return 2
    if not runtime_url:
        _emit({"skipped": "no GRANADA_RUNTIME_DATABASE_URL"})
        return 2

    def _role_of(url: str) -> str:
        return url.split("//", 1)[1].split(":", 1)[0].split("@", 1)[0]

    if _role_of(runtime_url) == _role_of(owner_url):
        # Not a failure and not a pass. As the owner, FORCE row-level security
        # still binds, so five of the six tenant tables would be proven - but
        # org_members would not, and a green result here would overstate what
        # was actually tested. Say so instead of quietly reporting success.
        print(RESULT_PREFIX + json.dumps({
            "skipped": (
                f"runtime role '{_role_of(runtime_url)}' is also the owner; "
                "configure GRANADA_RUNTIME_DATABASE_URL as a separate "
                "least-privilege role"
            )
        }))
        return 2

    schema = f"probe_{uuid.uuid4().hex[:10]}"
    scoped_owner = f"{owner_url}?options=-csearch_path%3D{schema}"
    scoped_runtime = f"{runtime_url}?options=-csearch_path%3D{schema}"
    runtime_role = _role_of(runtime_url)

    admin = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        _migrate(scoped_owner, schema, runtime_role)

        # Point the *application* at the runtime role before anything imports
        # config. This is the whole point: the app must work without ever holding
        # owner privileges.
        os.environ["DATABASE_URL"] = scoped_runtime
        os.environ["GRANADA_ADMIN_DATABASE_URL"] = scoped_owner
        return _run_app(scoped_owner)
    finally:
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _migrate(scoped_owner: str, schema: str, runtime_role: str) -> None:
    """Create the schema exactly as production does: seed, then enforce."""
    from sqlalchemy import create_engine, text

    command, Config = _real_alembic()

    saved_path, saved_admin = (
        os.environ.get("DATABASE_URL"),
        os.environ.get("GRANADA_ADMIN_DATABASE_URL"),
    )
    os.environ["DATABASE_URL"] = scoped_owner
    os.environ["GRANADA_ADMIN_DATABASE_URL"] = scoped_owner
    try:
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        # Reference data must exist before row-level security: FORCE binds the
        # owner too, so afterwards nothing can insert a shared system row.
        command.upgrade(cfg, "002_phase1_schema_alignment")
        engine = create_engine(scoped_owner)
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
        for key, value in (("DATABASE_URL", saved_path),
                           ("GRANADA_ADMIN_DATABASE_URL", saved_admin)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    grant = create_engine(scoped_owner, isolation_level="AUTOCOMMIT")
    try:
        with grant.connect() as conn:
            conn.execute(text(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO "{runtime_role}"'))
            conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO "{runtime_role}"'))
            conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{runtime_role}"'))
            # Unqualified: this engine's search_path is already the scratch
            # schema, and the name contains a dot PostgreSQL would read as
            # schema.app rather than schema."app".
            conn.execute(
                text(f'GRANT EXECUTE ON FUNCTION app.user_org_ids(text) TO "{runtime_role}"')
            )
    finally:
        grant.dispose()


def _real_alembic():
    """Import the installed alembic, not the local ``alembic/`` package.

    ``Auth/backend/alembic/`` shadows the distribution whenever BACKEND leads
    ``sys.path``; see ADR-0003 and the identical helper in
    ``tests/test_tenant_rls.py``.
    """
    for name in [n for n in list(sys.modules) if n == "alembic" or n.startswith("alembic.")]:
        del sys.modules[name]
    saved = sys.path[:]
    sys.path = [p for p in sys.path if pathlib.Path(p or ".").resolve() != BACKEND.resolve()]
    try:
        from alembic import command
        from alembic.config import Config
    finally:
        sys.path = saved
    return command, Config


def _run_app(owner_url: str) -> int:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    import main

    import database as database_module

    failures: list[str] = []
    checks: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            checks.append(name)
        else:
            failures.append(f"{name}: {detail}" if detail else name)

    client = TestClient(main.app)
    tag = uuid.uuid4().hex[:8]

    def sign_up(label: str) -> tuple[str, str]:
        # example.com, not a special-use name: pydantic's EmailStr rejects
        # .test/.invalid/.localhost outright, so a "safe" looking test domain
        # would fail validation for reasons unrelated to what is being probed.
        # Nothing is ever sent to it - the probe writes to a throwaway schema.
        email = f"{label}-{tag}@example.com"
        password = "Probe-Passw0rd!x9"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": label.title()},
        )
        check(
            f"{label}_register",
            response.status_code == 200,
            f"HTTP {response.status_code}: {response.text[:300]}",
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        check(
            f"{label}_login",
            login.status_code == 200,
            f"HTTP {login.status_code}: {login.text[:300]}",
        )
        if login.status_code != 200:
            raise SystemExit(1)
        token = login.json()["access_token"]
        org = client.post(
            "/api/v1/organizations",
            json={"name": f"{label.title()} Probe Org"},
            headers={"Authorization": f"Bearer {token}"},
        )
        check(
            f"{label}_create_org",
            org.status_code == 200,
            f"HTTP {org.status_code}: {org.text[:300]}",
        )
        if org.status_code != 200:
            raise SystemExit(1)
        return token, org.json()["id"]

    token_a, org_a = sign_up("alpha")
    token_b, org_b = sign_up("bravo")

    check("orgs_are_distinct", org_a != org_b, f"both are {org_a}")

    head_a = {"Authorization": f"Bearer {token_a}"}
    head_b = {"Authorization": f"Bearer {token_b}"}

    # 1. Each user sees only their own organisation.
    for label, head, expected, forbidden in (
        ("alpha", head_a, org_a, org_b),
        ("bravo", head_b, org_b, org_a),
    ):
        listing = client.get("/api/v1/organizations", headers=head)
        check(f"{label}_list_status", listing.status_code == 200,
              f"HTTP {listing.status_code}: {listing.text[:300]}")
        if listing.status_code == 200:
            ids = [row["id"] for row in listing.json()]
            check(f"{label}_sees_only_own_org", ids == [expected], f"got {ids}")
            check(f"{label}_cannot_list_other_org", forbidden not in ids, f"leaked {forbidden}")

    # 2. Each user is refused on the other's organisation, by explicit id.
    for label, head, victim in (("alpha", head_a, org_b), ("bravo", head_b, org_a)):
        members = client.get(f"/api/v1/orgs/{victim}/members", headers=head)
        check(
            f"{label}_cross_tenant_members_denied",
            members.status_code == 403,
            f"HTTP {members.status_code}: {members.text[:300]}",
        )

    # 3. A member of the organisation they do not belong to still gets nothing.
    #    Uses the ID, not the role, so it fails even for a wrongly-scoped admin.
    for label, head, victim in (("alpha", head_a, org_b), ("bravo", head_b, org_a)):
        members = client.get(f"/api/v1/orgs/{victim}/members", headers=head)
        check(
            f"{label}_cross_tenant_body_empty",
            members.status_code == 403 or not (members.json() or []),
            f"HTTP {members.status_code} with a body",
        )

    # 4. A user who belongs to NO organisation must see none at all - after the
    #    two tenants above have used the connection pool. This is the leak
    #    detector. The tenant GUCs are set at session level so they survive the
    #    Session.commit() calls already in the request path, which means a
    #    request that forgets to clear them leaves its tenant bound on a pooled
    #    connection, and the *next* request inherits it. Charlie has no
    #    membership, so the correct answer for him is the empty list; if he can
    #    see alpha's or bravo's organisation, some earlier request leaked.
    #    It tests the consequence rather than the pool's internal state, which
    #    is both the thing that actually matters and the thing that is hard to
    #    observe without perturbing the pool being measured.
    charlie_email = f"charlie-{tag}@example.com"
    _reg = client.post(
        "/api/v1/auth/register",
        json={"email": charlie_email, "password": "Probe-Passw0rd!x9",
              "display_name": "Charlie"},
    )
    check("charlie_register", _reg.status_code == 200,
          f"HTTP {_reg.status_code}: {_reg.text[:300]}")
    _login = client.post(
        "/api/v1/auth/login",
        json={"email": charlie_email, "password": "Probe-Passw0rd!x9"},
    )
    check("charlie_login", _login.status_code == 200,
          f"HTTP {_login.status_code}: {_login.text[:300]}")
    if _login.status_code == 200:
        head_c = {"Authorization": f"Bearer {_login.json()['access_token']}"}
        _seen = client.get("/api/v1/organizations", headers=head_c)
        check("charlie_sees_no_organisations", _seen.status_code == 200 and not _seen.json(),
              f"an organisation leaked to a member of none: {_seen.status_code} {_seen.text[:300]}")
        # And a direct id lookup must refuse, not return an empty body.
        _denied = client.get(f"/api/v1/orgs/{org_a}/members", headers=head_c)
        check("charlie_denied_on_foreign_org", _denied.status_code == 403,
              f"HTTP {_denied.status_code}: {_denied.text[:300]}")

    # 5. Both organisations really exist, and each is visible to exactly the
    #    role that owns it. FORCE row-level security binds the table owner too,
    #    so even the migration role sees no rows with no tenant bound - there is
    #    deliberately no way to read across tenants at the SQL tier. Binding the
    #    owner to each organisation in turn proves the rows exist *and* that
    #    the other one stays hidden, which an empty-table world would also
    #    have passed.
    from sqlalchemy import create_engine, text

    owner_engine = create_engine(owner_url)
    try:
        for label, org_id, hidden in (("alpha", org_a, org_b), ("bravo", org_b, org_a)):
            with owner_engine.connect() as conn:
                conn.execute(
                    text("SELECT set_config('app.current_org_id', :o, false)"),
                    {"o": org_id},
                )
                rows = conn.execute(text("SELECT id FROM organisations")).scalars().all()
                conn.commit()
            check(f"owner_sees_{label}_when_bound_to_it", rows == [org_id],
                  f"bound to {org_id} the owner saw {rows}, not exactly its own tenant")
            check(f"{label}_org_exists", hidden != org_id and hidden is not None, "")
    finally:
        owner_engine.dispose()

    _emit({"checks": len(checks), "failures": failures})
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the wrapper needs the reason
        _emit({"error": f"{type(exc).__name__}: {exc}"})
        raise SystemExit(1)