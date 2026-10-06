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

import os
import pathlib
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
    _scratch = tempfile.mkdtemp(prefix="granada-test-db-")
    os.environ["DATABASE_URL"] = f"sqlite:///{(pathlib.Path(_scratch) / 'test.db').as_posix()}"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
