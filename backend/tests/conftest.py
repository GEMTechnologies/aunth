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
