"""Every subprocess call must be bounded, and nothing may capture through an unmanaged pipe.

THE CLASS OF DEFECT
-------------------
`subprocess.run(cmd, capture_output=True, timeout=N)` **does not bound anything**. On Windows its
timeout path is `kill()` then `communicate()` *with no timeout*, and a child that spawned a
grandchild has handed that grandchild the pipes. Killing the child leaves the grandchild holding
them, so the untimed drain blocks until the grandchild exits on its own.

Measured at **120.3 seconds against a 3-second timeout**, while the security scan hung for
thirteen minutes on a `timeout=300`.

Three tools had **no `timeout=` at all**, which is worse because nothing even appears to bound
them:

| Tool | Command | What a hang means |
|---|---|---|
| `security_scan.py` | `uvx pip-audit` | a scan nobody runs |
| `backup_restore_check.py` | `pg_dump`, `pg_restore` | the backup verification silently never completes |
| `redis_restart_probe.py` | `Stop-Service Memurai` | **a probe that stopped Redis and never started it again** |

WHAT THIS ENFORCES
------------------
1. Every `subprocess.run` / `check_output` / `call` / `check_call` passes `timeout=`. These are
   short-lived commands; there is no case where a bound is wrong.
2. No `subprocess.Popen` captures through a pipe, except the two that drain it deliberately on a
   thread and kill the process — declared below with reasons, because a PIPE plus no drain is the
   hang shape.

Checked with `ast`, not with grep: a regex over source text cannot tell a call from a comment or a
docstring, and this repository's fix discusses `subprocess.run(... timeout=...)` at length in its
own documentation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: Files scanned. Everywhere a subprocess can be launched.
SEARCH_ROOTS = (
    ROOT / "tools",
    ROOT / "Auth" / "backend",
)
SKIP_PARTS = {".venv", "__pycache__", "node_modules", ".git", ".uv-work", ".uv-tools"}

#: Calls that must always carry a timeout.
MUST_BE_BOUNDED = {"run", "check_output", "call", "check_call"}

#: `subprocess.Popen` sites allowed to capture through a pipe, each with the reason.
#:
#: A pipe is not wrong by itself - it is wrong when nothing bounds it and nobody drains it. These
#: two launch a server that is *meant* to outlive the call, drain it on a thread, and kill it.
PIPE_ALLOWED: dict[str, str] = {
    "tools/api_smoke_test.py": (
        "launches uvicorn, which is meant to outlive the call. It drains stdout on a daemon "
        "thread and terminates the process in __exit__, and its readiness wait is bounded."
    ),
    "tools/load_test.py": (
        "the same: a server launched for the duration of a measurement, drained on a thread and "
        "terminated afterwards."
    ),
    "tools/live_journey.py": (
        "the same shape again: it launches uvicorn for one customer journey, drains stdout on a "
        "daemon thread, bounds the readiness wait, and terminates the process in __exit__. This "
        "entry was added because the guard CAUGHT it - which is the guard working, not a nuisance."
    ),
    "tools/bounded_subprocess.py": (
        "the bounded runner itself. It uses a FILE rather than a pipe in run_bounded; the "
        "Popen in kill_tree captures taskkill, which cannot outlive itself."
    ),
}


def _python_files() -> list[Path]:
    found: list[Path] = []
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            found.append(path)
    return sorted(found)


def _imported_from_subprocess(tree: ast.AST) -> set[str]:
    """Names brought into scope by `from subprocess import ...`.

    A bare `call(...)` is only a subprocess call if it was imported from `subprocess`. Without
    this, a LOCAL function named `call` matches - which is exactly what happened in
    `test_fleet_operations.py:431`, where `call` is a closure. A guard that reports a non-issue
    is a guard that gets switched off.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _call_name(node: ast.Call, imported: set[str]) -> str | None:
    """`subprocess.run` -> `run`, and a bare `run(...)` imported from subprocess -> `run`."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "subprocess":
            return func.attr
        return None
    if isinstance(func, ast.Name) and func.id in imported:
        return func.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.keyword | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword
    return None


_IMPORT_CACHE: dict[Path, set[str]] = {}


def _names_for(path: Path) -> set[str]:
    """Names imported from `subprocess` in this file."""
    if path not in _IMPORT_CACHE:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            _IMPORT_CACHE[path] = _imported_from_subprocess(tree)
        except (SyntaxError, OSError):
            _IMPORT_CACHE[path] = set()
    return _IMPORT_CACHE[path]


def _calls() -> list[tuple[Path, ast.Call]]:
    found: list[tuple[Path, ast.Call]] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        imported = _imported_from_subprocess(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node, imported)
                if name in MUST_BE_BOUNDED or name == "Popen":
                    found.append((path, node))
    return found


# ===========================================================================
def test_the_scan_finds_the_calls():
    """So the assertions below cannot pass by finding nothing.

    A scan that matched no calls would satisfy every rule trivially. This pins a floor: the
    repository launches subprocesses in three tools and several tests.
    """
    calls = _calls()
    assert len(calls) >= 10, f"only {len(calls)} subprocess calls found; the scan is broken"
    files = {path for path, _ in calls}
    assert any("tools" in str(path) for path in files), "no tool was scanned"


def test_every_short_lived_subprocess_call_has_a_timeout():
    """THE rule. A command that can hang must say how long it may take.

    This is what the three tools got wrong, and none of them had any bound at all - so the
    failure was not a too-short timeout, it was a scan that never ended and a probe that left a
    service stopped.
    """
    missing: list[str] = []
    for path, node in _calls():
        if _call_name(node, set()) not in MUST_BE_BOUNDED and _call_name(
            node, _names_for(path)
        ) not in MUST_BE_BOUNDED:
            continue
        if _keyword(node, "timeout") is None:
            missing.append(
                f"{path.relative_to(ROOT)}:{node.lineno}  {_call_name(node, _names_for(path))}(...)"
            )
    assert not missing, (
        "these subprocess calls have no timeout, so nothing bounds them:\n  "
        + "\n  ".join(missing)
        + "\n\nUse tools/bounded_subprocess.run_bounded or run_checked: `timeout=` on "
        "subprocess.run does not bound a call whose child spawned a grandchild."
    )


def test_no_popen_captures_through_an_undeclared_pipe():
    """A PIPE plus nothing draining it is the exact shape that blocked for 120 seconds.

    `subprocess.Popen` is legitimate - a server is meant to outlive the call. Capturing its
    output through a pipe is legitimate too, *if* something drains it. What is not legitimate is
    a pipe nobody manages, which is what makes the drain block.
    """
    offenders: list[str] = []
    for path, node in _calls():
        if _call_name(node, _names_for(path)) != "Popen":
            continue
        uses_pipe = _keyword(node, "capture_output") is not None
        for keyword in node.keywords:
            if keyword.arg in {"stdout", "stderr"}:
                value = ast.unparse(keyword.value)
                if "PIPE" in value:
                    uses_pipe = True
        if not uses_pipe:
            continue
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative not in PIPE_ALLOWED:
            offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, (
        "these Popen calls capture through a pipe that is not declared as drained:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither drain it on a thread and kill the process, or redirect to a file as "
        "bounded_subprocess.run_bounded does, or declare it in PIPE_ALLOWED with a reason."
    )


def test_the_pipe_allowlist_is_justified_and_not_stale():
    """An entry without a reason is an entry nobody agreed to, and a stale one hides a removal."""
    for relative, reason in PIPE_ALLOWED.items():
        assert (ROOT / relative).is_file(), f"{relative} is declared but does not exist"
        assert len(reason.strip()) >= 40, (
            f"{relative}'s reason is too short to be a decision: {reason!r}"
        )

    # Every declared file must actually still contain a Popen, or the entry is stale.
    for relative in PIPE_ALLOWED:
        path = ROOT / relative
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_popen = any(
            isinstance(node, ast.Call) and _call_name(node, set()) == "Popen"
            for node in ast.walk(tree)
        )
        assert has_popen, (
            f"{relative} is declared in PIPE_ALLOWED but no longer calls Popen, so the entry "
            "is stale and would pre-authorise a pipe that reappears later"
        )


def test_the_bounded_runner_redirects_to_a_file():
    """The mechanism, asserted where it lives.

    `run_bounded` must not use a pipe: that is the whole fix, and "simplifying" it back is the
    regression this file exists to catch.
    """
    source = (ROOT / "tools" / "bounded_subprocess.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    runner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_bounded"
    )
    text = ast.unparse(runner)
    assert "stdout=sink" in text, "run_bounded no longer redirects output to a file"
    assert "TemporaryFile" in text, "run_bounded no longer uses a temporary file"
    assert "PIPE" not in text, "run_bounded captures through a pipe, which is the defect"
    assert "timeout=timeout" in text, "run_bounded no longer waits under a timeout"
