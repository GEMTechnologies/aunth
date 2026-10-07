"""Run the PostgreSQL request-path probe in a fresh interpreter.

The probe itself lives in ``pg_request_path_probe.py``. See that file for why it
cannot be a normal pytest module: ``config.settings`` and ``database.engine``
are import-time singletons, and the rest of the suite pins ``DATABASE_URL`` to
SQLite before anything is imported. A subprocess is the only way to boot the
application against the real database and the real runtime role.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PROBE = BACKEND / "tests" / "pg_request_path_probe.py"

#: Must match ``RESULT_PREFIX`` in the probe. A named line, not a "line that
#: starts with a brace": the probe also prints an indented human copy, whose
#: opening line is a bare ``{``. Scanning for that yielded an empty dict, and
#: this wrapper went on to assert that 25 successful checks had produced no
#: results at all.
RESULT_PREFIX = "GRANADA_PROBE_RESULT "


def _configured() -> bool:
    from dotenv import dotenv_values

    values = dotenv_values(BACKEND / ".env")
    for name in ("GRANADA_ADMIN_DATABASE_URL", "GRANADA_RUNTIME_DATABASE_URL"):
        if values.get(name):
            return True
    return False


@pytest.mark.skipif(
    not _configured(),
    reason=(
        "the request-path probe needs GRANADA_ADMIN_DATABASE_URL (owner) and "
        "GRANADA_RUNTIME_DATABASE_URL (granada_app) in Auth/backend/.env or the "
        "environment"
    ),
)
def test_request_path_isolates_tenants() -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        timeout=300,
    )

    payload = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX):])
            break

    context = (
        f"exit={result.returncode}\n"
        f"stdout:\n{result.stdout[-4000:]}\n"
        f"stderr:\n{result.stderr[-4000:]}"
    )

    assert payload is not None, (
        "the probe printed no result line, so it did not finish; "
        "its assertions were never reached\n" + context
    )

    if result.returncode == 2:
        pytest.skip(payload.get("skipped", "probe declined to run"))

    assert result.returncode == 0, (
        "tenant isolation probe failed\n"
        f"failures: {json.dumps(payload.get('failures', payload), indent=2)}\n"
        + context
    )

    # Two assertions that must both hold for this test to mean anything: the
    # probe really ran some checks, and every one of them passed. Asserting only
    # "no failures" would be satisfied by a probe that checked nothing.
    assert payload.get("checks"), "the probe reported zero checks; it did not really run"
    assert not payload.get("failures"), payload["failures"]