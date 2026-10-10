"""The host-side runner's bounds, pinned.

WHY THIS FILE EXISTS

`tools/browser_worker_runner.py` exists because the container cannot run a browser - measured, not
assumed. It takes the same leased jobs and executes them host-side.

The three properties that make it safe are all properties a future edit could quietly remove, so they
are asserted here:

  * **one session at a time** - the ceiling is credential blast radius, not memory
  * **it never submits** - `submission_authorised` is forced False, and authority lives elsewhere
  * **a lease is required** - a job already held is left alone, because running it anyway is how a job
    executes twice

The process-level tests drive the runner's own failure paths, which must degrade to UNCERTAIN for the
same reason the container's invoker does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.path.insert(0, str(BACKEND / "tools"))

import browser_worker_runner as runner  # noqa: E402


# ===========================================================================
# THE BOUNDS
# ===========================================================================
def test_concurrency_is_one():
    """Asserted as a value, because `MAX_CONCURRENT_SESSIONS` is documentation until something checks
    it. Measured memory would allow ~3; the ceiling is that one organisation's credentials should be in
    one browser at a time."""
    assert runner.MAX_CONCURRENT_SESSIONS == 1


def test_the_job_type_matches_the_workflow_engine():
    """A rename on either side must fail here rather than silently claiming nothing - a runner that
    finds no work looks exactly like a runner with no work to do."""
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    assert f'WORKFLOW_BROWSER_TASK = "{runner.BROWSER_JOB_TYPE}"' in source


def test_the_worker_path_points_at_the_real_worker():
    assert Path(runner.WORKER_PROCESS).name == "browser_worker.py"
    assert Path(runner.WORKER_PROCESS).parent.name == "tools"


# ===========================================================================
# THE WORKER LAUNCH - failure paths
# ===========================================================================
def test_a_worker_that_exits_non_zero_is_uncertain(tmp_path, monkeypatch):
    """Same rule as the container's invoker: a worker that died may have acted before it did."""
    script = tmp_path / "die.py"
    script.write_text("import sys\nsys.exit(4)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "WORKER_PROCESS", str(script))

    report = runner.run_worker({}, timeout_seconds=20)
    assert report["status"] == "UNCERTAIN"
    assert report["outcome_certain"] is False
    assert report["problems"][0]["kind"] == "WORKER_EXIT"


def test_a_worker_that_times_out_is_uncertain(tmp_path, monkeypatch):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "WORKER_PROCESS", str(script))

    report = runner.run_worker({}, timeout_seconds=1)
    assert report["status"] == "UNCERTAIN"
    assert report["outcome_certain"] is False
    assert report["problems"][0]["kind"] == "WORKER_TIMEOUT"


def test_a_worker_returning_junk_is_uncertain(tmp_path, monkeypatch):
    """Unparseable output is not success. Treating it as one is how a submission gets claimed without
    evidence."""
    script = tmp_path / "junk.py"
    script.write_text("print('not json at all')\n", encoding="utf-8")
    monkeypatch.setattr(runner, "WORKER_PROCESS", str(script))

    report = runner.run_worker({}, timeout_seconds=20)
    assert report["status"] == "UNCERTAIN"
    assert report["problems"][0]["kind"] == "WORKER_UNPARSEABLE_OUTPUT"


def test_a_worker_report_is_passed_through_unchanged(tmp_path, monkeypatch):
    """The runner is a transport, not a judge. Whatever the worker reports is what is recorded - the
    interpretation belongs to `browser_invocation._interpret`, in one place."""
    script = tmp_path / "ok.py"
    script.write_text(
        "import json,sys\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'status': 'COMPLETED', 'echoed_job': req.get('job_id')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "WORKER_PROCESS", str(script))

    report = runner.run_worker({"job_id": "job-42"}, timeout_seconds=20)
    assert report["status"] == "COMPLETED"
    assert report["echoed_job"] == "job-42", "the request did not reach the worker on stdin"


def test_the_task_never_appears_on_the_command_line(tmp_path, monkeypatch):
    """It goes on stdin, so nothing tenant-identifying shows up in the process table."""
    script = tmp_path / "argv.py"
    script.write_text(
        "import json,sys\nprint(json.dumps({'status': 'COMPLETED', 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "WORKER_PROCESS", str(script))

    report = runner.run_worker({"org_id": "org-secret", "form_data": {"x": "top-secret"}}, timeout_seconds=20)
    assert report["argv"] == []


# ===========================================================================
# THE CLAIM PATH
# ===========================================================================
def test_run_once_returns_none_when_there_is_nothing_to_do(monkeypatch):
    class EmptyDB:
        def execute(self, *a, **k):
            class R:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return R()

    assert runner.run_once(EmptyDB(), worker_id="w", execute=lambda *a, **k: {}) is None


def test_run_once_claims_at_most_one_job_per_call():
    """THE concurrency bound, asserted against the code path rather than the constant.

    A loop that claimed a list and ran it would open as many browsers as the list was long. This
    asserts the shape: one iteration, one job.
    """
    source = (BACKEND / "tools" / "browser_worker_runner.py").read_text(encoding="utf-8")
    body = source[source.index("def run_once(") : source.index("def main(")]
    # `find_browser_jobs(..., limit=5)` may list several; the loop must return on the first claim.
    assert "return {" in body
    assert body.count("run_worker_fn(") == 1


def test_submission_is_forced_off_in_the_request():
    """THE authority bound. The runner cannot grant what it was not given, and does not try."""
    source = (BACKEND / "tools" / "browser_worker_runner.py").read_text(encoding="utf-8")
    assert 'request["submission_authorised"] = False' in source
    assert 'request["dry_run"] = True' in source


def test_run_once_passes_false_regardless_of_the_payload():
    """A payload claiming authority must not survive into the worker request. Asserted by driving the
    real function against a fake database and capturing what the worker was handed."""
    captured: dict = {}

    class FakeAttempt:
        id = "attempt-1"

    class FakeLedger:
        def __init__(self, db):
            pass

        def claim(self, **kwargs):
            return FakeAttempt()

    class FakeRow:
        org_id = "org-a"
        payload = {"submission_authorised": True, "job_id": "job-1"}

    class FakeDB:
        def execute(self, *a, **k):
            class R:
                def scalars(self):
                    return self

                def all(self):
                    return ["job-1"]

                def scalar_one_or_none(self):
                    return FakeRow()

            return R()

    import events.ledger as ledger_module

    monkeypatch_target = ledger_module.JobLedger
    ledger_module.JobLedger = FakeLedger  # type: ignore[assignment]
    try:
        runner.run_once(
            FakeDB(),
            worker_id="w",
            execute=lambda request, **k: captured.update(request) or {"status": "COMPLETED"},
        )
    finally:
        ledger_module.JobLedger = monkeypatch_target  # type: ignore[assignment]

    assert captured["submission_authorised"] is False, (
        "a payload claiming submission authority reached the worker; the runner must force it off"
    )
    assert captured["dry_run"] is True
    assert captured["job_id"] == "job-1"
    assert captured["org_id"] == "org-a"
