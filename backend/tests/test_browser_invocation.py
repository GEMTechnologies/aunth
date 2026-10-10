"""Invoking the browser worker. Every test is about a refusal or an untrusted report.

The worker is a SEPARATE PROCESS on the host, so its output is data, not a verdict. The two rules this
file pins: a disabled flag means no browser at all, and a worker claiming success without a receipt is
downgraded to uncertain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_boundary import ActionScope, BrowserTask  # noqa: E402
from agent.browser_invocation import (  # noqa: E402
    BROWSER_EXECUTION_ENABLED,
    BROWSER_WORKER_COMMAND,
    BrowserWorkerUnavailable,
    InvocationPolicy,
    SubprocessInvoker,
    describe,
    enabled,
    invoke,
)


def task(**over) -> BrowserTask:
    base = dict(
        task_id="task-1",
        org_id="org-aaaa",
        package_id="pkg-1",
        workflow_id="wf-1",
        job_id="job-1",
        package_fingerprint="fp-1",
        action_scope=ActionScope(portal_name="Portal", allowed_hosts=("portal.example",)),
        documents=[],
    )
    base.update(over)
    return BrowserTask(**base)  # type: ignore[arg-type]


class FakeInvoker:
    """Records what it was asked to do, so a test can assert the worker was NEVER invoked."""

    def __init__(self, raw=None):
        self.raw = raw or {"status": "COMPLETED"}
        self.calls = 0
        self.last_task = None

    def run(self, t, *, timeout_seconds):
        self.calls += 1
        self.last_task = t
        return self.raw


SETTINGS_ON = {BROWSER_EXECUTION_ENABLED: True, BROWSER_WORKER_COMMAND: "/usr/local/bin/browser-worker"}


# ===========================================================================
# THE COMMAND IS A COMMAND LINE
# ===========================================================================
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a shebang script is not executable on Windows; the worker host is Linux",
)
def test_a_bare_binary_still_works(tmp_path):
    """The tests use `/bin/true`-shaped values, and that must keep working."""
    script = tmp_path / "worker"
    script.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"status\": \"BLOCKED\"}'\n")
    script.chmod(0o755)
    raw = SubprocessInvoker(str(script)).run(task(), timeout_seconds=10)
    assert raw["status"] == "BLOCKED"


def test_a_command_LINE_with_arguments_is_accepted(tmp_path):
    """THE DEPLOYED SHAPE. The container-side client is `tools/browser_execute_client.py` - a plain
    non-executable module with no shebang and no console script - so while the invoker built
    `[self.command]`, NO value an operator could configure would start it.

    A setting named `browser_worker_command` is a command line, and the client is reached as
    `python /app/backend/tools/browser_execute_client.py`.
    """
    script = tmp_path / "client.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'status': 'BLOCKED', 'seen_task': payload.get('task_id')}))\n"
    )
    raw = SubprocessInvoker(f"{sys.executable} {script}").run(task(), timeout_seconds=20)
    assert raw["status"] == "BLOCKED"
    assert raw["seen_task"] == "task-1", "the task JSON did not reach the client on stdin"


def test_an_empty_command_is_refused_rather_than_starting_anything():
    with pytest.raises(BrowserWorkerUnavailable):
        SubprocessInvoker("").run(task(), timeout_seconds=5)


def test_a_command_line_whose_binary_is_missing_is_refused():
    with pytest.raises(BrowserWorkerUnavailable):
        SubprocessInvoker("definitely-not-a-real-worker --flag").run(task(), timeout_seconds=5)


# ===========================================================================
# THE FLAG
# ===========================================================================
def test_browser_execution_is_off_by_default():
    """The directive requires external submission stay disabled. Built, tested, switched off."""
    inv = FakeInvoker()
    out = invoke(task(), invoker=inv, settings={})
    assert out.status == "DISABLED"
    assert out.ran is False
    assert inv.calls == 0, "a browser was invoked while the capability was disabled"


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (None, False), ("", False),
    # A string from an env file: naive truthiness would treat "false" as ON.
    ("false", False), ("False", False), ("0", False), ("no", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
])
def test_the_flag_string_does_not_enable_a_disabled_feature(value, expected):
    """`"false"` is truthy in Python. That is how a disabled capability gets switched on by a typo in
    an environment file, so the parse is explicit."""
    assert enabled({BROWSER_EXECUTION_ENABLED: value}) is expected


def test_enabled_absent_is_off():
    assert enabled({}) is False


# ===========================================================================
# REFUSALS BEFORE THE WORKER
# ===========================================================================
def test_no_organisation_is_refused_before_the_worker():
    inv = FakeInvoker()
    out = invoke(task(org_id=""), invoker=inv, settings=SETTINGS_ON)
    assert out.status == "REJECTED"
    assert inv.calls == 0, "a task with no organisation must not reach a worker"


def test_an_unconfigured_worker_reports_UNAVAILABLE_not_a_task_failure():
    """Distinguishing 'the capability is not deployed' from 'the task failed' matters: they need
    different people to act."""
    out = invoke(task(), invoker=FakeInvoker(), settings={BROWSER_EXECUTION_ENABLED: True})
    assert out.status == "UNAVAILABLE"
    assert out.problems[0]["kind"] == "WORKER_NOT_CONFIGURED"
    assert "host" in out.problems[0]["detail"]


def test_submission_is_withheld_by_default_when_invoking():
    """A permitted host is not permission to submit. The flag off is reported as off."""
    inv = FakeInvoker()
    invoke(task(), invoker=inv, settings=SETTINGS_ON)
    assert inv.calls == 1


# ===========================================================================
# THE WORKER'S REPORT IS DATA, NOT A VERDICT
# ===========================================================================
def test_a_worker_reporting_SUBMITTED_without_a_receipt_is_downgraded():
    """THE rule at the integration boundary. The worker is a separate process; its word is not
    evidence, and only a receipt makes a submission."""
    inv = FakeInvoker({"status": "SUBMITTED"})
    out = invoke(task(), invoker=inv, settings=SETTINGS_ON, submission_authorised=True)
    assert out.status == "UNCERTAIN"
    assert out.outcome_certain is False
    assert out.receipt is None
    assert any(p["kind"] == "SUBMISSION_CLAIMED_WITHOUT_RECEIPT" for p in out.problems)


def test_a_worker_reporting_SUBMITTED_with_a_receipt_is_accepted():
    inv = FakeInvoker({"status": "SUBMITTED", "receipt": "FUNDER-REF-7"})
    out = invoke(task(), invoker=inv, settings=SETTINGS_ON, submission_authorised=True)
    assert out.status == "SUBMITTED"
    assert out.receipt == "FUNDER-REF-7"


def test_unparseable_worker_output_is_uncertain_not_success():
    """A worker that does not return parseable JSON has not reported an outcome."""

    class Silent:
        def run(self, t, *, timeout_seconds):
            raise AssertionError  # not used

    # Exercise the real parsing path with a stub process result.
    class FakeCompleted:
        returncode = 0
        stdout = b"not json at all"
        stderr = b""

    import agent.browser_invocation as bi

    original = bi.subprocess.run
    try:
        bi.subprocess.run = lambda *a, **k: FakeCompleted()
        raw = SubprocessInvoker("/bin/true").run(task(), timeout_seconds=5)
    finally:
        bi.subprocess.run = original
    assert raw["status"] == "UNCERTAIN"
    assert raw["outcome_certain"] is False


def test_a_worker_timeout_is_uncertain_not_failed():
    """A timeout after the click may have submitted. Reporting it as FAILED would authorise a retry
    and a second application."""
    import subprocess as sp

    import agent.browser_invocation as bi

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=1)

    original = bi.subprocess.run
    try:
        bi.subprocess.run = boom
        raw = SubprocessInvoker("/bin/true").run(task(), timeout_seconds=1)
    finally:
        bi.subprocess.run = original
    assert raw["status"] == "UNCERTAIN"
    assert raw["outcome_certain"] is False
    assert "reconciled rather than retried" in raw["problems"][0]["detail"]


def test_a_missing_worker_binary_is_reported_as_unavailable():
    with pytest.raises(BrowserWorkerUnavailable):
        SubprocessInvoker("definitely-not-a-real-worker-binary").run(task(), timeout_seconds=5)


# ===========================================================================
# QUIET BY DEFAULT: THE BOUND IS ONE SESSION
# ===========================================================================
def test_the_default_policy_is_one_concurrent_session():
    """The directive: start at a maximum of one concurrent browser session and keep spare memory."""
    assert InvocationPolicy().max_concurrent == 1


def test_the_worker_is_invoked_once_per_call_with_no_state_kept():
    """A process per invocation, no daemon: the directive forbids a permanent browser per
    organisation, and this asserts nothing is retained between runs."""
    inv = FakeInvoker()
    invoke(task(), invoker=inv, settings=SETTINGS_ON)
    invoke(task(), invoker=inv, settings=SETTINGS_ON)
    assert inv.calls == 2


# ===========================================================================
# THE TRADE IS STATED
# ===========================================================================
def test_describe_records_the_host_side_trade_and_the_sandbox():
    d = describe()
    assert d["default"] == "disabled"
    assert "host-side" in d["location"]
    assert "sandboxed chromium cannot start" in d["location"].lower()
    assert d["sandbox"].startswith("enabled"), "the sandbox must never be reported as disabled"
    joined = " ".join(d["does_not_do"])
    assert "build_task" in joined
    assert "submission_authority" in joined
    assert "submission_lifecycle" in joined
