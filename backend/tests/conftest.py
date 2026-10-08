"""Shared pytest configuration.

Argon2 is deliberately expensive (64 MB, 3 passes) in production. Running it at
full cost across the suite added over a minute of wall clock for no extra
coverage, so the cost is lowered here.

This does NOT weaken the suite: ``test_argon2id_uses_configured_cost`` reads
``settings.argon2_*`` and compares them against the encoded hash, so it still
proves the configured parameters reach the hash. Only the absolute numbers
change.

These variables are set before ``config`` is imported by any test module.

Database isolation
------------------
The suite pins ``DATABASE_URL`` to a throwaway SQLite file. ``.env`` is a real
developer artefact and points at a live PostgreSQL instance, and the boot tests
call ``create_tables()``; without this pin the suite would create and drop
tables in whatever database ``.env`` names. Environment variables outrank
``.env`` in pydantic-settings, so setting it here is sufficient to win.

Set ``GRANADA_TEST_USE_REAL_DB=1`` to opt out and deliberately run the suite
against the configured database.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import sys
import tempfile

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def import_real_alembic():
    """Import the installed alembic distribution, never ``Auth/backend/alembic``.

    The migration script directory is also a package: it contains an
    ``__init__.py``, so while the backend root leads ``sys.path`` it shadows the
    installed distribution and ``from alembic import command`` resolves to the
    migration folder (recorded as ADR-0003; the directory was kept because
    ``alembic.ini`` points at it).

    Hiding the backend root for the duration of the import is not sufficient on
    its own, and the reason is worth stating because the failure it caused was
    order-dependent and looked like a migration defect:

    Two test modules each repaired ``sys.modules`` only for the duration of
    their own import, and one of them put the saved shadow *back* afterwards
    (``sys.modules.update(saved)``). That reinstates the migration folder as
    ``alembic`` while the real ``alembic.context`` stays cached, so the next
    ``from alembic import context`` inside ``env.py`` resolves against a
    half-shadowed package and ``context.config`` raises ``AttributeError``.
    ``test_schema_drift.py`` therefore passed when run alone and errored when
    run after ``test_tenant_rls.py``.

    The invariant this establishes: once the real distribution is imported it
    stays in ``sys.modules`` for the rest of the process. Nothing depends on
    importing the migration folder as a package - Alembic loads ``env.py`` and
    every revision by filesystem path.
    """
    for name in [n for n in list(sys.modules) if n == "alembic" or n.startswith("alembic.")]:
        del sys.modules[name]

    saved_path = sys.path[:]
    sys.path[:] = [
        p for p in sys.path
        if not (p and pathlib.Path(p).resolve() == BACKEND.resolve())
    ]
    try:
        from alembic import command
        from alembic.config import Config
    finally:
        sys.path[:] = saved_path

    resolved = pathlib.Path(getattr(command, "__file__", "") or "").resolve()
    if resolved.parent.parent == BACKEND.resolve():
        raise RuntimeError(
            f"the local ./alembic package shadowed the distribution: {resolved}"
        )
    return command, Config


# Establish the real distribution before any test module is imported, so the
# suite does not depend on the order pytest happens to collect modules in.
ALEMBIC_COMMAND, ALEMBIC_CONFIG = import_real_alembic()

os.environ.setdefault("ARGON2_MEMORY", "8192")
os.environ.setdefault("ARGON2_TIME", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")
os.environ.setdefault("APP_ENV", "test")

if not os.environ.get("GRANADA_TEST_USE_REAL_DB"):
    # NOTE: tempfile.gettempdir() walks TEMP/TMP/TMPDIR and then CWD, testing each
    # for writability, and silently falls back to CWD if none qualify. Under the
    # agent sandbox the normal temp dir is not usable, so this lands the scratch
    # database inside the source tree instead of the system temp dir. That is why
    # this registers explicit cleanup rather than relying on the OS: without it
    # every test run leaks a directory into Auth/backend, and a tree that
    # accumulates artefacts on each run makes "git status is clean" meaningless.
    #
    # Before creating ours, sweep anything a previous killed run left behind, so
    # a crashed run does not silently accumulate across sessions.
    _parent = pathlib.Path(BACKEND)
    for _stale in _parent.glob("granada-test-db-*"):
        if _stale.is_dir():
            shutil.rmtree(_stale, ignore_errors=True)

    _scratch = tempfile.mkdtemp(prefix="granada-test-db-")
    _scratch_path = pathlib.Path(_scratch)
    os.environ["DATABASE_URL"] = f"sqlite:///{(_scratch_path / 'test.db').as_posix()}"

    def _remove_scratch() -> None:
        # Backstop only. The session fixture below is the primary cleanup path,
        # because by the time atexit runs the SQLAlchemy engine may still hold the
        # SQLite file open: Windows locks open files, rmtree fails, and
        # ignore_errors=True would swallow that failure and leave the tree dirty
        # with no indication why. This backstop therefore reports instead of
        # hiding.
        try:
            shutil.rmtree(_scratch_path)
        except FileNotFoundError:
            pass  # the session fixture already removed it; this is success
        except OSError as exc:  # pragma: no cover - diagnostic path
            print(
                f"\n[conftest] could not remove scratch dir {_scratch_path}: {exc}\n"
                "            it is still locked by an open handle, most likely the "
                "SQLite engine.\n            This is a test-hygiene defect, not a "
                "cosmetic one: a tree that gains artefacts on every run makes "
                "'git status is clean'\n            useless as evidence.",
                file=sys.stderr,
            )

    atexit.register(_remove_scratch)


@pytest.fixture(scope="session", autouse=True)
def _scratch_database_cleanup():
    """Dispose the engine, then remove the scratch database directory.

    Runs at end of session, while the interpreter is still healthy and before
    any final teardown has closed the handle underneath us.
    """
    yield

    scratch = os.environ.get("DATABASE_URL", "")
    if scratch.startswith("sqlite:///") and "granada-test-db-" in scratch:
        try:
            from database import engine

            engine.dispose()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[conftest] engine.dispose() failed: {exc}", file=sys.stderr)

        for stale in pathlib.Path(BACKEND).glob("granada-test-db-*"):
            shutil.rmtree(stale, ignore_errors=True)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Test database construction
# ---------------------------------------------------------------------------
# WHY THIS EXISTS - a measured 800x speed-up, and the explanation of a suite that
# went from ~2 minutes to 1h40m.
#
# Each `db` fixture used to call `models.Base.metadata.create_all(engine)` against
# a fresh file under tmp_path. Profiling that, per test:
#
#   create_all -> sqlite file on F:        3,828 ms
#   create_all -> in-memory + StaticPool     125 ms
#   copy a prebuilt schema file                3 ms
#
# Two compounding causes. `tempfile.gettempdir()` returns the *repository
# directory* under this sandbox, so every test wrote its database to F:. And the
# schema is now 38 tables with **203 indexes**, each index being its own disk
# write; 38 tables with no indexes cost 0.87s against 3.7s with them.
#
# So the slowdown was not machine load - the first hypothesis, and the wrong one.
# It was a fixed per-test cost that grew with the schema. At 3.8s per test across
# 537 tests that is 34 minutes before any test body runs, which is the regression.
#
# The fix builds the schema ONCE per session into a template file and copies it
# per test: 3 ms against 3,828 ms, an ~800x reduction, with no change to what the
# tests exercise - it is still a real file-backed SQLite database.
_TEMPLATE: dict[str, str] = {}


def _schema_template() -> str:
    """Build the schema once, into a file that every test copies."""
    if "path" in _TEMPLATE:
        return _TEMPLATE["path"]

    import models
    from sqlalchemy import create_engine

    template_dir = pathlib.Path(tempfile.mkdtemp(prefix="granada-schema-"))
    template_path = template_dir / "schema.db"

    engine = create_engine(f"sqlite:///{template_path.as_posix()}")
    try:
        models.Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    _TEMPLATE["path"] = template_path.as_posix()
    _TEMPLATE["dir"] = str(template_dir)
    return _TEMPLATE["path"]


def make_sqlite_db(tmp_path, name: str = "test.db"):
    """A fresh, migrated SQLite database and a Session bound to it.

    Returns ``(engine, session)``. The caller closes the session and disposes the
    engine; the file itself lives in ``tmp_path`` and is cleaned up by pytest.
    """
    import shutil

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    template = _schema_template()
    db_path = pathlib.Path(tmp_path) / name
    shutil.copyfile(template, db_path)

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    # **Enforce foreign keys, deterministically.**
    #
    # SQLite has foreign keys OFF by default, so whether a constraint fired depended
    # on which module had run first and issued `PRAGMA foreign_keys=ON`. That is how a
    # test that fabricated a `send_intent_id` passed alone and failed in the full
    # suite with an IntegrityError - and the inconsistency is worse than the bug,
    # because it means the suite enforces different rules depending on order.
    #
    # Turning it on always means the tests exercise the constraints the deployed
    # database actually has: the composite agent/org keys, the dedupe uniqueness, and
    # the foreign keys that make a fabricated row impossible.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session = sessionmaker(bind=engine, future=True)()
    return engine, session


@pytest.fixture(scope="session", autouse=True)
def _schema_template_cleanup():
    """Remove the template directory at the end of the session."""
    yield
    template_dir = _TEMPLATE.get("dir")
    if template_dir:
        shutil.rmtree(template_dir, ignore_errors=False)


# ---------------------------------------------------------------------------
# Leave app.user_org_ids() pointing at a schema that exists.
#
# THE OUTAGE THIS PREVENTS
# ------------------------
# `alembic/versions/003_row_level_security.py` creates the tenant-resolution bootstrap as:
#
#     CREATE OR REPLACE FUNCTION app.user_org_ids(p_user_id text)
#     ...
#     SET search_path = {current_schema()}, pg_temp
#     AS $$ SELECT m.org_id FROM {current_schema()}.org_members m ... $$
#
# Resolving from `current_schema()` is deliberate, and the migration's docstring says why: the
# PostgreSQL tests migrate into a scratch schema, so hard-coding `public` would be wrong there.
#
# **But the function is a single GLOBAL object in the shared `app` schema.** The RLS tests migrate
# with `search_path=probe_xxxxxxxx`, so `CREATE OR REPLACE` overwrites the PRODUCTION function and
# pins it to that scratch schema - and the fixture then drops the schema.
#
# Measured on the live database afterwards:
#
#     relation "probe_e0e9e3483f.org_members" does not exist
#
# raised inside `get_current_user` and reported to the client as **401 "Authentication failed"**, so
# every user was locked out and the symptom pointed at their password. 1227 tests passed throughout,
# because each one builds its world before exercising it and none calls the function afterwards.
#
# WHAT THIS FIXTURE DOES
# ----------------------
# It cannot stop the overwrite - that happens inside a module-scoped fixture this one cannot reach
# into. What it guarantees is that **the suite does not LEAVE the database broken**: at the end of the
# session, if the pinned schema no longer exists, it is restored to `public`.
#
# It reports loudly rather than silently repairing, because a suite that quietly fixes damage it
# caused is a suite nobody learns from.
# ---------------------------------------------------------------------------

#: The canonical definition, kept in step with migration 003 and `tools/repair_bootstrap_function.sql`.
_BOOTSTRAP_FUNCTION_SQL = """
CREATE FUNCTION app.user_org_ids(p_user_id text)
RETURNS SETOF text
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $$
    SELECT m.org_id
    FROM public.org_members m
    WHERE m.user_id = p_user_id
      AND p_user_id IS NOT NULL
$$;
COMMENT ON FUNCTION app.user_org_ids(text) IS
    'Org ids the given user belongs to. SECURITY DEFINER bootstrap helper: it exposes only '
    'memberships the caller already owns, and never row content. Calling it proves identity, '
    'not tenancy - the caller must still set app.current_org_id.';
REVOKE ALL ON FUNCTION app.user_org_ids(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.user_org_ids(text) TO granada_app;
"""


def _admin_postgres_url() -> str | None:
    """The owner URL, or None when this machine has no PostgreSQL configured."""
    for name in ("GRANADA_ADMIN_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(name, "")
        if value.startswith("postgres"):
            return value
    env_file = pathlib.Path(BACKEND) / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("GRANADA_ADMIN_DATABASE_URL="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value if value.startswith("postgres") else None
    return None


def _bootstrap_pin_is_stale(engine) -> str | None:
    """The dead schema name if `app.user_org_ids` is pinned to a schema that does not exist."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT array_to_string(p.proconfig, ',') "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'app' AND p.proname = 'user_org_ids'"
            )
        ).scalar()
        if not row or "search_path" not in row:
            return None
        schemas = [
            part.strip().strip('"')
            for part in row.split("=", 1)[1].split(",")
            if part.strip() and part.strip() != "pg_temp"
        ]
        for schema in schemas:
            present = conn.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname = :n"), {"n": schema}
            ).scalar()
            if not present:
                return schema
    return None


def restore_bootstrap_function(engine) -> str | None:
    """Re-pin `app.user_org_ids` to `public` if it is pinned to a schema that does not exist.

    Returns the dead schema name it repaired, or None if nothing needed repairing.

    Called from the teardown of every fixture that migrates into a scratch schema, so the function
    is healthy for the REST of the session and not merely at the end of it. The session-scoped
    fixture below is the backstop for anything that slips through.
    """
    from sqlalchemy import text

    stale = _bootstrap_pin_is_stale(engine)
    if stale is None:
        return None

    print(
        f"\n[conftest] app.user_org_ids was pinned to {stale!r}, which no longer exists.\n"
        "[conftest] Every authenticated request would fail with 401 'Authentication failed'.\n"
        "[conftest] Restoring it to `public` - see tests/test_bootstrap_function.py.",
        file=sys.stderr,
    )
    with engine.connect() as conn:
        conn.execute(text("DROP FUNCTION IF EXISTS app.user_org_ids(text)"))
        conn.execute(text(_BOOTSTRAP_FUNCTION_SQL))

    remaining = _bootstrap_pin_is_stale(engine)
    if remaining:
        print(
            f"[conftest] RESTORE FAILED: still pinned to {remaining!r}. "
            "Run tools/repair_bootstrap_function.sql.",
            file=sys.stderr,
        )
    else:
        print("[conftest] app.user_org_ids restored.", file=sys.stderr)
    return stale


@pytest.fixture(scope="session", autouse=True)
def _restore_bootstrap_function_after_the_suite():
    """Backstop: restore `app.user_org_ids` if the tests left it pinned to a dropped schema.

    See the block comment above: without this, running the PostgreSQL tests locks every user out of
    the database they ran against, and reports it as a bad password.
    """
    yield

    url = _admin_postgres_url()
    if not url:
        return

    try:
        from sqlalchemy import create_engine
    except Exception:  # pragma: no cover - import failure is not this fixture's problem
        return

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        restore_bootstrap_function(engine)
    except Exception as exc:  # noqa: BLE001 - never fail a suite during teardown
        print(f"[conftest] bootstrap-function restore failed: {exc}", file=sys.stderr)
    finally:
        engine.dispose()
