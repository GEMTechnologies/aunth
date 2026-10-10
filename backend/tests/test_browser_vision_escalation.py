"""The browser runtime's vision escalation: a reading is recorded, never relied on.

WHY THIS FILE EXISTS

`perception.read_page_with_vision` was written and had no caller. `browser_runtime` captures a
screenshot into `PageState.screenshot_ref` and `from_page_state` carries it into an Observation - and
nothing joined them, so the browser's visual channel still ended at a boolean.

The decisions worth testing here are about what the runtime does with a reading:

  * a vision FAILURE never stops the run - the structural path is still valid
  * a reading NEVER becomes a field outcome, so it cannot be written into a form as a fact
  * an obstacle becomes a PROBLEM, so a caller can park the workflow with a legible reason
  * no reader, or no picture, means no call at all
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_boundary import ActionScope, BrowserTask  # noqa: E402
from agent.browser_runtime import (  # noqa: E402
    ActionResult,
    BrowserRuntime,
    Failure,
    PageState,
    Step,
)
from agent.perception import Channel, PerceivedValue  # noqa: E402


class ScriptedProvider:
    """A provider whose observations and actions are scripted by the test."""

    def __init__(self, observations: list[PageState], actions: Optional[list[ActionResult]] = None):
        self._observations = list(observations)
        self._actions = list(actions or [])
        self.acted: list[tuple] = []
        self.closed = False

    def launch(self, *, profile_dir: str, headless: bool = True) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def observe(self) -> PageState:
        if len(self._observations) > 1:
            return self._observations.pop(0)
        return self._observations[0]

    def act(self, step: Step, target: str, value: Optional[str] = None) -> ActionResult:
        self.acted.append((step, target, value))
        if self._actions:
            return self._actions.pop(0)
        return ActionResult(ok=True, page=self._observations[0])

    def screenshot(self) -> str:
        return self._observations[0].screenshot_ref


def _task() -> BrowserTask:
    return BrowserTask(
        task_id="task-1",
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


def _page(*, picture: bool = True, fields: Optional[dict] = None) -> PageState:
    return PageState(
        url="https://portal.example/apply",
        title="Apply",
        fields=fields or {},
        controls=[],
        validation_messages=[],
        screenshot_ref="data:image/png;base64,AAAA" if picture else "",
        captured_at=datetime.now(timezone.utc) if picture else None,
    )


def _run(provider, *, vision_reader=None):
    runtime = BrowserRuntime(provider, vision_reader=vision_reader)
    return runtime.run(_task(), values={}, uploads={}, skip_launch=True)


def _value(name, value, channel=Channel.VISION):
    return PerceivedValue(
        name=name, value=value, channel=channel, evidence_ref="data:image/png;base64,AAAA"
    )


# ===========================================================================
# WHEN IT IS CALLED
# ===========================================================================
def test_no_reader_means_no_call_and_no_problem():
    """Absent by default: `browser_execution` is 0 in production, and a run must not depend on a
    credential to complete work the structure already answers."""
    report = _run(ScriptedProvider([_page()]))
    assert report.vision_observations == []
    assert not [p for p in report.problems if "VISION" in p["kind"]]


def test_no_picture_means_no_call():
    """A page with no screenshot cannot be read visually, so the reader must not even be invoked."""
    called: list = []
    report = _run(
        ScriptedProvider([_page(picture=False)]),
        vision_reader=lambda page: called.append(page) or [],
    )
    assert called == []
    assert report.vision_observations == []


def test_a_picture_with_a_reader_invokes_it():
    called: list = []
    _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: called.append(page) or [_value("Amount", "500")],
    )
    assert len(called) == 1, "the reader was not invoked for a page that has a picture"


# ===========================================================================
# WHAT IS RECORDED
# ===========================================================================
def test_a_reading_is_recorded_with_its_channel():
    report = _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: [_value("Amount", "500")],
    )
    assert len(report.vision_observations) == 1
    entry = report.vision_observations[0]
    assert entry["name"] == "Amount"
    assert entry["value"] == "500"
    assert entry["channel"] == "VISION"


def test_a_reading_never_becomes_a_field_outcome():
    """THE SAFETY PROPERTY AT THE RUNTIME. `field_outcomes` is what a form is filled from, and
    `perception.FACT_CHANNELS` excludes VISION - so a number read off a screenshot must not appear
    there under any key."""
    report = _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: [_value("amount", "500000"), _value("legal_name", "Example")],
    )
    assert report.vision_observations, "nothing was recorded"
    assert "amount" not in report.field_outcomes
    assert "legal_name" not in report.field_outcomes
    assert "500000" not in str(report.field_outcomes)


def test_the_serialised_report_carries_the_observations_separately():
    report = _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: [_value("Amount", "500")],
    )
    payload = report.to_dict()
    assert payload["vision_observations"]
    assert payload["field_outcomes"] == {}


# ===========================================================================
# AN OBSTACLE IS A PROBLEM, NOT AN ACTION
# ===========================================================================
def test_an_obstacle_is_surfaced_as_a_problem():
    """A CAPTCHA or an expired session is exactly what the DOM omits. Recording it as a problem lets
    the caller park the workflow legibly instead of filling a form that cannot be submitted."""
    report = _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: [_value("obstacle", "Your session has expired. Sign in again.")],
    )
    obstacles = [p for p in report.problems if p["kind"] == "VISUAL_OBSTACLE"]
    assert len(obstacles) == 1
    assert "expired" in obstacles[0]["detail"]


def test_an_obstacle_is_also_kept_as_an_observation():
    """Both, not either: the problem is for the caller's decision, the observation for the evidence."""
    report = _run(
        ScriptedProvider([_page()]),
        vision_reader=lambda page: [_value("obstacle", "CAPTCHA present")],
    )
    assert any(v["name"] == "obstacle" for v in report.vision_observations)


# ===========================================================================
# A VISION FAILURE IS NOT A RUN FAILURE
# ===========================================================================
def test_a_reader_that_raises_does_not_stop_the_run():
    """THE decision that matters most. A model that is down, out of budget or refusing an image must
    not turn a form-filling task into a failure - the structural path is still valid."""

    def exploding(page):
        raise RuntimeError("model unavailable")

    report = _run(ScriptedProvider([_page()]), vision_reader=exploding)
    assert report.status in ("COMPLETED", "BLOCKED", "FAILED")
    kinds = [p["kind"] for p in report.problems]
    assert "VISION_UNAVAILABLE" in kinds
    assert report.vision_observations == []


def test_the_vision_failure_names_the_cause_without_the_page():
    """The detail goes into a durable report. It must name the failure type and must not carry page
    content, which can include an organisation's data."""

    def exploding(page):
        raise ValueError("secret-looking detail")

    report = _run(ScriptedProvider([_page()]), vision_reader=exploding)
    detail = next(p["detail"] for p in report.problems if p["kind"] == "VISION_UNAVAILABLE")
    assert "ValueError" in detail
    assert "secret-looking detail" not in detail


def test_a_reader_returning_none_is_tolerated():
    """A reader with nothing to report must not crash the loop."""
    report = _run(ScriptedProvider([_page()]), vision_reader=lambda page: None)
    assert report.vision_observations == []


def test_a_malformed_value_does_not_crash_the_loop():
    """The reader is injected by a caller, so its output is not guaranteed well-formed. A value
    missing `channel` must still not stop a browser run."""

    class Odd:
        name = "Amount"
        value = "500"
        # no channel, no evidence_ref, no confidence

    report = _run(ScriptedProvider([_page()]), vision_reader=lambda page: [Odd()])
    assert len(report.vision_observations) == 1
    assert report.vision_observations[0]["channel"] is None
