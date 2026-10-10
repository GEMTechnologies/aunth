"""The browser worker's failure modes must degrade SAFELY, and one of them is real today.

WHY THIS FILE EXISTS

The full path from a job to a running browser has been traced to its boundary and the boundary is
measured, not assumed:

    /app/backend/tools/browser_worker.py   exists in the image
    playwright / browser_use / stagehand   NOT importable in the container
    Chromium on disk in the container      absent
    host eval venv mounted into container  no - only postgres has a volume

So a job routed through `_handle_browser_execution` today spawns the worker INSIDE the container, where
its dependencies do not exist. Verified by running it:

    {"status": "UNCERTAIN", "outcome_certain": false,
     "problems": [{"kind": "WORKER_CRASH", "detail": "ModuleNotFoundError: No module named 'playwright'"}],
     "provider": "playwright-chromium", "sandbox": "enabled"}

**The result is `UNCERTAIN`, not `FAILED`.** That distinction is the whole point of the directive's
submission rule: a worker that crashed might have acted before it crashed, so the honest state is "we do
not know", and "we do not know" must never be retried automatically as though nothing happened.

These tests pin that behaviour, because the moment it degrades to a plain failure or an automatic retry,
the safety property is gone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import browser_invocation  # noqa: E402
from agent.browser_invocation import BrowserWorkerUnavailable, SubprocessInvoker  # noqa: E402

#: The three process-level tests below drive a POSIX shell script as the worker command. On Windows,
#: `shutil.which` will not resolve a `.sh` path and `command.startswith("/")` is False for a backslash
#: path, so the invoker refuses before spawning - which is correct behaviour and the wrong test.
#:
#: The BEHAVIOUR under test is platform-independent; only the harness is not. The deployment is Linux
#: and these were verified there. Skipping with the reason stated is better than a test that asserts
#: something different from what it claims.
_posix_worker = pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives a POSIX shell script as the worker; verified on the Linux deployment",
)


def test_a_command_that_does_not_exist_refuses_before_spawning():
    """The empty-command case. `shutil.which("")` is None and `"".startswith("/")` is False, so the
    invoker raises rather than spawning nothing and calling it a crash."""
    with pytest.raises(BrowserWorkerUnavailable):
        SubprocessInvoker("").run(_task(), timeout_seconds=5)


def test_a_named_command_missing_from_path_refuses_with_a_useful_message():
    """The message must say WHERE the worker runs, because that is the actual fault: an operator
    configuring a host path is configuring something this container cannot see."""
    with pytest.raises(BrowserWorkerUnavailable) as caught:
        SubprocessInvoker("definitely-not-a-real-worker-binary").run(_task(), timeout_seconds=5)
    assert "not found on PATH" in str(caught.value)
    assert "host" in str(caught.value)


@_posix_worker
def test_a_worker_that_crashes_reports_uncertain_not_failed(tmp_path):
    """THE safety property at the process boundary.

    A worker exiting non-zero may have acted before it died. Reporting FAILED would invite a retry, and
    a retry of a possibly-landed submission is the failure this whole project is built to avoid.
    """
    crasher = tmp_path / "crasher.sh"
    crasher.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    crasher.chmod(0o755)

    outcome = SubprocessInvoker(str(crasher)).run(_task(), timeout_seconds=10)

    assert outcome["status"] == "UNCERTAIN", (
        "a crashed worker was reported as a definite outcome; if it had reached the submission step "
        "before dying, a retry would file a second application"
    )
    assert outcome["outcome_certain"] is False


@_posix_worker
def test_a_crashed_worker_names_the_crash_without_leaking_the_task(tmp_path):
    """The report is durable. It must name the failure and must not carry the submitted task, which
    contains an organisation's form data."""
    crasher = tmp_path / "crasher.sh"
    crasher.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    crasher.chmod(0o755)

    outcome = SubprocessInvoker(str(crasher)).run(_task(), timeout_seconds=10)
    kinds = [p["kind"] for p in outcome["problems"]]
    assert "WORKER_CRASH" in kinds
    assert "Example Foundation" not in json.dumps(outcome)


@_posix_worker
def test_a_worker_that_times_out_is_uncertain():
    """A timeout is the case the docstring calls out: the worker may have reached the click. It must
    not be retryable as though it did nothing."""
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    handle.write("#!/bin/sh\nsleep 30\n")
    handle.close()
    slower = Path(handle.name)
    slower.chmod(0o755)
    try:
        outcome = SubprocessInvoker(str(slower)).run(_task(), timeout_seconds=1)
        assert outcome["status"] == "UNCERTAIN"
        assert outcome["outcome_certain"] is False
        assert any(p["kind"] == "WORKER_TIMEOUT" for p in outcome["problems"])
    finally:
        slower.unlink(missing_ok=True)


def test_the_real_container_boundary_is_recorded_not_guessed():
    """The measurement that makes this file necessary, asserted as a fact about the deployment rather
    than a comment someone can drift away from.

    The worker file is in the image; its dependencies are not. If a future change adds playwright to the
    production image this assertion will fail, and that is correct - it is a deliberate deployment
    decision that deserves a deliberate test update.
    """
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "playwright" not in requirements, (
        "playwright has been added to the production image; the browser worker is intended to run "
        "host-side, and this changes the isolation argument in docs/browser-execution-boundary.md"
    )


def _task():
    from agent.browser_boundary import ActionScope, BrowserTask

    return BrowserTask(
        task_id="task-crash",
        org_id="org-a",
        package_id="pkg-1",
        workflow_id="wf-1",
        job_id="job-1",
        package_fingerprint="fp-1",
        action_scope=ActionScope(
            portal_name="test-portal",
            allowed_hosts=["portal.example"],
            allowed_path_prefixes=["/"],
        ),
        form_data={"organisation_name": "Example Foundation"},
        documents=[],
    )
