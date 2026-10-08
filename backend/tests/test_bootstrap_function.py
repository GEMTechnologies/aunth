"""The bootstrap function must be pinned to a schema that EXISTS.

THE OUTAGE THIS GUARDS
----------------------
`app.user_org_ids` is the SECURITY DEFINER bootstrap that resolves which organisations a user
belongs to. Migration 003 creates it as:

    CREATE OR REPLACE FUNCTION app.user_org_ids(p_user_id text)
    ...
    SET search_path = {current_schema()}, pg_temp
    AS $$ SELECT m.org_id FROM {current_schema()}.org_members m ... $$

Resolving from `current_schema()` is deliberate — the docstring explains that the test suite
migrates into a scratch schema, so hard-coding `public` would be wrong there.

**But the function is one GLOBAL object in the shared `app` schema.** `test_tenant_rls.py` migrates
with `search_path=probe_xxxxxxxx`, so `CREATE OR REPLACE` overwrites the production function and pins
it to that scratch schema — and the test then drops the schema.

The result, measured on the live database:

    relation "probe_e0e9e3483f.org_members" does not exist

...raised inside `get_current_user`, reported to the client as **HTTP 401 "Authentication failed"**,
and every user was locked out. 1227 tests passed throughout, because the suite builds its world
before exercising it.

WHAT THIS TEST DOES ABOUT IT
----------------------------
It cannot stop the overwrite — that is the test suite's design and fixing it needs the fixture to
restore the function, which is a separate change. What it does is make the failure LOUD: a pinned
schema that no longer exists is now a **failing test** instead of a silent 401 that looks like a bad
password.

Run it after the PostgreSQL suite and it tells you immediately whether you just broke authentication.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _admin_url() -> str | None:
    value = os.environ.get("GRANADA_ADMIN_DATABASE_URL")
    if value and value.startswith("postgres"):
        return value
    env_file = BACKEND / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("GRANADA_ADMIN_DATABASE_URL="):
            candidate = line.split("=", 1)[1].strip().strip('"').strip("'")
            return candidate if candidate.startswith("postgres") else None
    return None


def _query(sql: str) -> list[str]:
    """Run SQL as the owner, outside the application's session settings."""
    url = _admin_url()
    assert url, "no PostgreSQL configured"
    password = url.rsplit("://", 1)[1].split(":")[1].split("@")[0]
    psql = r"C:\Program Files\PostgreSQL\15\bin\psql.exe"
    result = subprocess.run(
        [psql, "-w", "-U", "granada_user", "-d", "granada_auth", "-h", "localhost",
         "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, env=dict(os.environ, PGPASSWORD=password), timeout=60,
    )
    output = (result.stdout or result.stderr).strip()
    return [line for line in output.splitlines() if line]


def test_the_bootstrap_function_is_pinned_to_a_schema_that_EXISTS():
    """THE regression.

    A pinned schema that has been dropped means `app.user_org_ids` raises for every caller, which
    surfaces as `401 Authentication failed` — a symptom that sends you looking at credentials.
    """
    if not _admin_url():
        pytest.skip("no PostgreSQL configured")

    rows = _query(
        "SELECT COALESCE(array_to_string(p.proconfig, ','), '') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'app' AND p.proname = 'user_org_ids'"
    )
    if not rows or not rows[0]:
        pytest.skip("app.user_org_ids is not installed")

    pinned = rows[0]
    assert "search_path" in pinned, (
        f"app.user_org_ids has no pinned search_path ({pinned!r}). A SECURITY DEFINER function "
        "must pin one, or it can be hijacked by the caller's search_path."
    )

    # `search_path=probe_abc, pg_temp` -> the schema names, minus pg_temp.
    schemas = [
        part.strip().strip('"')
        for part in pinned.split("=", 1)[1].split(",")
        if part.strip() and part.strip() != "pg_temp"
    ]
    assert schemas, f"could not read a schema out of {pinned!r}"

    for schema in schemas:
        exists = _query(
            f"SELECT count(*) FROM pg_namespace WHERE nspname = '{schema}'"
        )
        assert exists and exists[0] == "1", (
            f"app.user_org_ids is pinned to schema {schema!r}, which DOES NOT EXIST. Every call "
            "raises, and every authenticated request fails with 401 'Authentication failed'. "
            "This is what running the PostgreSQL test suite does to the database it runs against: "
            "migration 003 overwrites the function with SET search_path = current_schema(), and the "
            "scratch schema is dropped afterwards. Repair with tools/repair_bootstrap_function.sql."
        )


def test_the_bootstrap_function_actually_runs():
    """Pinned to an existing schema is not the same as working.

    A `FROM public.org_members` that does not resolve raises the same way a dropped pin does, so
    call it — with an id that matches nothing, which must return no rows rather than raise.
    """
    if not _admin_url():
        pytest.skip("no PostgreSQL configured")

    rows = _query(
        "SELECT count(*) FROM app.user_org_ids('00000000-0000-0000-0000-000000000000')"
    )
    assert rows and rows[0] == "0", (
        f"app.user_org_ids did not run cleanly: {rows!r}. Any error here becomes a 401 for every "
        "authenticated request."
    )


def test_the_function_is_still_SECURITY_DEFINER_and_locked_down():
    """The tenant-resolution path must not have been weakened while being repaired.

    It answers "which orgs does this user belong to" without a tenant bound, so it is the one place
    the RLS model deliberately steps around itself — and `EXECUTE` must therefore stay revoked from
    PUBLIC.
    """
    if not _admin_url():
        pytest.skip("no PostgreSQL configured")

    rows = _query(
        "SELECT p.prosecdef::text || '|' || "
        "COALESCE((SELECT bool_or(a.grantee = 0) FROM aclexplode(p.proacl) a), true)::text "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'app' AND p.proname = 'user_org_ids'"
    )
    if not rows:
        pytest.skip("app.user_org_ids is not installed")

    security_definer, public_grant = rows[0].split("|")
    assert security_definer == "true", (
        "app.user_org_ids is no longer SECURITY DEFINER, so it can no longer resolve memberships "
        "before a tenant is bound - and tenant establishment deadlocks behind its own policy"
    )
    assert public_grant == "false", (
        "EXECUTE on app.user_org_ids is granted to PUBLIC, which makes it a general "
        "'list every org' oracle rather than a proof of identity"
    )
