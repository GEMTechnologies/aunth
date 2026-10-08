"""Port allocation: one source of truth, and the code agrees with it.

These exist because the port map had drifted into three disagreeing copies. A
documentation file that nothing reads drifts again, so the assertions here are
about the *code* matching the file, not about the file being internally tidy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent.parent
OPS = REPO / "ops"

if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ports  # noqa: E402


def test_ports_yaml_exists_and_parses():
    assert (OPS / "ports.yaml").is_file(), "ops/ports.yaml is the single source of truth"
    assert ports.by_name("auth-service") is not None


def test_auth_service_is_the_port_the_code_actually_binds():
    """The template said 8001 and the code said 8000.

    The code wins: the frontend proxy and the OAuth redirect URIs registered
    with Google and GitHub already point at 8000, so moving the running value to
    satisfy a template would break working integrations.
    """
    entry = ports.by_name("auth-service")
    assert entry["port"] == 8000
    assert entry["implemented"] is True


def test_no_two_services_share_a_port():
    """This is the defect the file was written to end.

    BILLING_SERVICE_PORT=8005 collided with the AI supervisor's de facto 8005
    while AI_SUPERVISOR_PORT=8006 sat beside it. Neither service has an
    entrypoint, so nothing ever failed - the collision was simply invisible.
    """
    entries = [
        *(ports._load().get("services") or []),
        *(ports._load().get("frontends") or []),
    ]
    seen: dict[int, str] = {}
    clashes = []
    for entry in entries:
        port = entry.get("port")
        name = entry.get("name")
        if port in seen:
            clashes.append(f"{name} and {seen[port]} both claim {port}")
        else:
            seen[port] = name
    assert clashes == [], f"port collisions: {clashes}"


def test_no_service_claims_a_reserved_port():
    """5432 and 6379 belong to PostgreSQL and Memurai."""
    reserved = set(ports.reserved_ports())
    taken = {
        e.get("port")
        for e in [*(ports._load().get("services") or []), *(ports._load().get("frontends") or [])]
    }
    overlap = sorted(reserved & taken)
    assert overlap == [], f"services claim ports reserved by other software: {overlap}"


def test_every_entry_declares_an_env_var_and_implemented_flag():
    """``implemented`` is what stops a plan being reported as a running service."""
    for section in ("services", "frontends"):
        for entry in ports._load().get(section) or []:
            assert entry.get("env"), f"{entry.get('name')} has no env var"
            assert "implemented" in entry, f"{entry.get('name')} has no implemented flag"


def test_evidence_is_required_where_implemented_is_true():
    """A claim of implementation without evidence is the thing this repo bans."""
    for section in ("services", "frontends"):
        for entry in ports._load().get(section) or []:
            if entry.get("implemented"):
                assert entry.get("evidence"), (
                    f"{entry.get('name')} claims implemented: true with no evidence field"
                )


def test_only_the_auth_service_is_implemented():
    """An honest boundary. Everything else has a main.py and nothing to run it.

    If this assertion starts failing because a service genuinely became
    runnable, that is good news and the test should be updated - but it must be
    updated deliberately, not by deleting the check.
    """
    assert ports.implemented() == ["auth-service", "auth-frontend"]
    assert "grants-service" in ports.planned()
    assert "api-gateway" in ports.planned()


def test_environment_variable_overrides_the_file(monkeypatch):
    """An operator must be able to move a service without editing the file."""
    monkeypatch.setenv("AUTH_SERVICE_PORT", "9123")
    assert ports.port_for("auth-service", 8000) == 9123


def test_a_non_numeric_environment_variable_falls_back(monkeypatch):
    monkeypatch.setenv("AUTH_SERVICE_PORT", "not-a-port")
    assert ports.port_for("auth-service", 8000) == 8000


def test_an_unknown_service_returns_the_default():
    assert ports.port_for("no-such-service", 7999) == 7999
    assert ports.by_name("no-such-service") is None


def test_a_missing_ports_file_does_not_crash(monkeypatch):
    """Documentation is not worth an outage."""
    monkeypatch.setattr(ports, "_cache", {})
    assert ports.port_for("auth-service", 8000) == 8000
    assert ports.implemented() == []


# ---------------------------------------------------------------------------
# The entrypoint itself
# ---------------------------------------------------------------------------
def _run_ast():
    """Parse ``run.py`` rather than grepping it.

    Grepping the source would match the module's own docstring, which describes
    the very defects being asserted against - so the test would fail on a
    correctly fixed file. The AST sees statements, not prose.
    """
    import ast

    return ast.parse((BACKEND / "run.py").read_text(encoding="utf-8"))


def test_run_py_has_no_relative_import():
    """The original could not run at all.

    It did ``from .config import settings``, so ``python run.py`` - which is how
    the README and the runbook both describe it - raised ImportError before
    reaching uvicorn. Nothing caught it because nobody ran it directly.
    """
    import ast

    relative = [
        node
        for node in ast.walk(_run_ast())
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0
    ]
    assert relative == [], (
        "run.py contains a relative import, which cannot resolve when the file "
        "is executed directly"
    )


def test_run_py_does_not_hardcode_the_bind_port():
    """``uvicorn.run(port=...)`` must receive a name, not an integer literal."""
    import ast

    calls = [
        node
        for node in ast.walk(_run_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert calls, "run.py no longer calls uvicorn.run"

    port_values = [
        kw.value
        for call in calls
        for kw in call.keywords
        if kw.arg == "port"
    ]
    assert port_values, "uvicorn.run is called without an explicit port"
    for value in port_values:
        assert isinstance(value, ast.Name), (
            f"the bind port is a literal; it must come from ops/ports.yaml or "
            f"the environment (found {ast.dump(value)})"
        )


def test_run_py_actually_executes_as_a_script():
    """The real proof, since the defect was 'it cannot run'.

    It is executed with ``--help``-style short-circuiting rather than by binding
    a socket: importing the module and resolving the port exercises exactly the
    path that used to raise ImportError, without starting a server in the test
    suite.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import run; print('PORT=', run._port()); print('DEFAULT=', run.DEFAULT_PORT)",
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows", "ARGON2_MEMORY": "8192",
             "ARGON2_TIME": "1", "ARGON2_PARALLELISM": "1", "APP_ENV": "test"},
             timeout=120,)
    assert result.returncode == 0, (
        f"run.py could not be imported: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "PORT= 8000" in result.stdout, result.stdout


def test_run_py_honours_the_environment_port():
    import importlib

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.environ['AUTH_SERVICE_PORT']='9123';"
            "import run; print('PORT=', run._port())",
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows", "ARGON2_MEMORY": "8192",
             "ARGON2_TIME": "1", "ARGON2_PARALLELISM": "1", "APP_ENV": "test"},
             timeout=120,)
    assert result.returncode == 0, result.stderr
    assert "PORT= 9123" in result.stdout, result.stdout


def test_a_bad_port_value_exits_with_a_clear_message():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.environ['AUTH_SERVICE_PORT']='http';"
            "import run; run._port()",
        ],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows", "ARGON2_MEMORY": "8192",
             "ARGON2_TIME": "1", "ARGON2_PARALLELISM": "1", "APP_ENV": "test"},
             timeout=120,)
    assert result.returncode != 0
    assert "must be an integer" in (result.stdout + result.stderr)
