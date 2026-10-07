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
