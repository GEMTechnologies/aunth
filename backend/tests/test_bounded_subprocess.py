"""A timeout that does not bound is worse than no timeout, because it is believed.

THE DEFECT
----------
`security_scan.py` ran its dependency audit as:

    subprocess.run([...], capture_output=True, text=True, timeout=300)

That call did not bound anything. On Windows the timeout path is `process.kill()` followed by
`communicate()` **with no timeout**, and `uvx` spawns `pip-audit` as a **grandchild that inherits
stdout/stderr**. Killing `uvx` leaves the grandchild holding the pipes, so the untimed drain
blocks until the grandchild exits on its own.

Measured: **120.3 seconds against a 3-second timeout**, and in the real scan a 300-second timeout
that never fired - a security scan that hangs. In CI that is a hung job; locally it is a scan
nobody runs.

THE FIX
-------
Output goes to a temporary **file**, never a pipe. Nothing can block on a file, so
`wait(timeout=)` bounds the call by construction, and the tree is killed best-effort afterwards
for hygiene.

These tests use the real shape of the bug: a parent that spawns a long-lived grandchild and then
exits immediately. They assert the call RETURNS, within a few seconds, which a pipe-based
implementation cannot do.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import security_scan  # noqa: E402
from security_scan import run_bounded  # noqa: E402

#: A parent that spawns a grandchild inheriting the pipes, then exits at once. This is exactly
#: the shape of `uvx pip-audit`, and it is what defeated the original timeout.
GRANDCHILD = (
    "import subprocess, sys;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
    "sys.exit(0)"
)

#: A child that itself outlives the timeout, so the timeout path is exercised rather than the
#: immediate-exit path.
SLEEPER = "import time; time.sleep(120)"

#: Generous: the assertions are "bounded", not "fast". A pipe-based implementation returns after
#: the grandchild's 120 seconds, so anything under 30 is unambiguous.
BOUND = 30.0


def test_a_surviving_grandchild_does_not_block_the_call():
    """THE regression.

    The direct child exits immediately; only the grandchild holds the output. With a pipe this
    blocks for the grandchild's full 120 seconds. With a file it returns at once.
    """
    start = time.time()
    _proc, timed_out = run_bounded([sys.executable, "-c", GRANDCHILD], timeout=5)
    elapsed = time.time() - start
    assert elapsed < BOUND, (
        f"run_bounded took {elapsed:.1f}s. A surviving grandchild is holding the output open, "
        "which means the timeout is not bounding anything - the exact defect this replaced."
    )
    # The direct child exited normally, so this is not a timeout: it is the pipe that was the
    # problem, not the duration.
    assert timed_out is False


def test_the_timeout_path_is_itself_bounded():
    """A child that genuinely outlives the timeout must still return, and say it timed out.

    This is the case the original code *appeared* to handle: `TimeoutExpired` was raised, and
    then the drain blocked anyway.
    """
    start = time.time()
    _proc, timed_out = run_bounded([sys.executable, "-c", SLEEPER], timeout=2)
    elapsed = time.time() - start
    assert timed_out is True, "a child that outlives the timeout must report the timeout"
    assert elapsed < BOUND, (
        f"run_bounded took {elapsed:.1f}s to give up on a sleeping child; the timeout is not "
        "bounding the call"
    )


def test_a_normal_command_still_returns_its_output():
    """The fix must not have broken the ordinary path, or the audit would silently see nothing.

    An audit that reports no advisories because it read no output is worse than one that fails:
    it looks like a pass.
    """
    proc, timed_out = run_bounded(
        [sys.executable, "-c", "print('advisories: none')"], timeout=30
    )
    assert timed_out is False
    assert proc is not None
    assert proc.returncode == 0
    assert "advisories: none" in (proc.stdout or "")


def test_a_missing_command_is_reported_rather_than_raising():
    """`uvx` absent is the ordinary state on a machine without it, and it must be a finding.

    The caller turns `None` into `AUDIT_DID_NOT_RUN` at HIGH severity. A crash here would
    instead abort the scan, and an aborted scan reports nothing.
    """
    proc, timed_out = run_bounded(["definitely-not-a-real-executable-xyz"], timeout=5)
    assert proc is None
    assert timed_out is False


def test_the_dependency_audit_goes_through_the_bounded_runner():
    """A behavioural guard, expressed structurally because the audit needs the network.

    Comments are stripped first: this repository has repeatedly had guards satisfied - or
    broken - by the text a file *says* rather than what it *does*, and the fix's own docstring
    discusses `subprocess.run(... timeout=...)` at length.
    """
    source = (TOOLS / "security_scan.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # Remove docstrings, which is where the explanation of the bug lives.
    import ast

    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body.pop(0)
    executable = ast.unparse(tree)

    assert "run_bounded(" in executable, (
        "the dependency audit no longer goes through run_bounded, so its timeout may not bound "
        "anything"
    )
    assert "pip-audit" in executable, "the audit invocation has moved or been removed"


def test_the_bounded_runner_does_not_use_a_pipe():
    """The mechanism, asserted directly: a pipe is what a surviving grandchild can hold open.

    If somebody "simplifies" this back to `capture_output=True`, the grandchild test above
    fails - but this states the reason, so the next reader does not have to rediscover it.
    """
    source = (TOOLS / "security_scan.py").read_text(encoding="utf-8")
    start = source.index("def run_bounded(")
    end = source.index("@dataclass", start)
    body = source[start:end]
    assert "stdout=sink" in body, "run_bounded no longer redirects output to a file"
    assert "capture_output" not in body, (
        "run_bounded captures through a pipe, which is the defect it exists to avoid"
    )
    assert "PIPE" not in body
