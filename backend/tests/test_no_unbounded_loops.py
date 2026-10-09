"""Regression tests for unbounded action loops.

WHY THIS FILE EXISTS
--------------------
In round 33 the runtime acquired an advance branch that clicked a progress control and had NO MEMORY
of having clicked it. On a page that does not change the same click was re-planned until the action
budget stopped it at sixty.

That is not a theoretical failure. On a live funder portal it is: click Continue, the page loads
slowly, re-observe still shows the old page, click again - sixty times, holding a browser session the
whole while. The action budget catching it is a BACKSTOP, not a design, and the directive names this
exact behaviour: "Do not repeatedly click, refresh or submit without a bounded recovery strategy."

The existing scenario tests pass with the guard in place, but they would also pass if the guard were
removed and the budget merely happened to be large enough. These tests assert the guard itself, so a
future change cannot quietly reintroduce the loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_boundary import ActionScope, BrowserTask  # noqa: E402
from agent.browser_runtime import (  # noqa: E402
    CONSEQUENTIAL,
    BrowserRuntime,
    ActionResult,
    PageState,
    RetryPolicy,
    Step,
    plan_next,
)

ORG = "org-loop"


def task(**over) -> BrowserTask:
    base = dict(
        task_id="t-loop", org_id=ORG, package_id="p", workflow_id="w", job_id="j",
        package_fingerprint="fp",
        action_scope=ActionScope(portal_name="P", allowed_hosts=("portal.example",)),
        documents=[],
    )
    base.update(over)
    return BrowserTask(**base)  # type: ignore[arg-type]


def stuck_page() -> PageState:
    """A page that offers Continue and NEVER changes - the condition that produced the loop."""
    return PageState(url="https://portal.example/step", fields={}, controls=["Continue"])


class StaticProvider:
    """Returns the same page forever and records every action.

    A provider that never advances is the worst case: the runtime sees the same structure every
    iteration and has no external signal that its click did nothing.
    """

    name = "static"

    def __init__(self):
        self.actions = []
        self.launched = self.closed = False

    def launch(self, *, profile_dir, headless=True):
        self.launched = True

    def close(self):
        self.closed = True

    def observe(self):
        return stuck_page()

    def act(self, step, target, value=None):
        self.actions.append((step, target))
        return ActionResult(ok=True, page=stuck_page())

    def screenshot(self):
        return ""


# ===========================================================================
# THE LOOP
# ===========================================================================
def test_an_advance_control_is_not_clicked_twice_on_an_unchanged_page():
    """THE regression. Without the guard this raises BrowserError('action budget exhausted')."""
    provider = StaticProvider()
    report = BrowserRuntime(provider, policy=RetryPolicy(max_total_actions=60)).run(
        task(), values={}, uploads={}
    )

    clicks = [a for a in provider.actions if a[0] is Step.CLICK]
    assert len(clicks) == 1, f"the advance control was clicked {len(clicks)} times"
    assert report.status == "BLOCKED", "a page that never changed must not be reported as complete"
    assert any("already clicked" in (p.get("detail") or "") for p in report.problems)


def test_the_loop_is_bounded_far_below_the_action_budget():
    """The guard stops it at one click, so the budget is never the thing that saves the run."""
    provider = StaticProvider()
    BrowserRuntime(provider, policy=RetryPolicy(max_total_actions=3)).run(
        task(), values={}, uploads={}
    )
    # If the budget were what stopped it, the run would have raised rather than returning BLOCKED.
    assert len([a for a in provider.actions if a[0] is Step.CLICK]) == 1


def test_a_stuck_page_blocks_rather_than_reporting_completion():
    """A COMPLETED here would be a false completion claim: the page never advanced."""
    provider = StaticProvider()
    report = BrowserRuntime(provider).run(task(), values={}, uploads={})
    assert report.status == "BLOCKED"
    assert report.status != "COMPLETED"


# ===========================================================================
# THE PLANNER'S CONTRACT, DIRECTLY
# ===========================================================================
def test_plan_next_advances_when_the_control_is_new():
    action = plan_next(
        stuck_page(), values={}, uploads={}, already_done=set(), declaration_accepted=False
    )
    assert action.step is Step.CLICK
    assert action.target == "Continue"


def test_plan_next_REFUSES_to_re_click_a_control_already_done():
    action = plan_next(
        stuck_page(), values={}, uploads={}, already_done={"Continue"}, declaration_accepted=False
    )
    assert action.step is Step.BLOCKED
    assert "already clicked" in action.because


def test_a_filled_field_is_not_refilled_either():
    """The same property for fields, so the pattern is general rather than special-cased."""
    page = PageState(
        url="https://p/a",
        fields={"name": {"type": "text", "required": True}},
        controls=["Continue"],
    )
    first = plan_next(page, values={"name": "N"}, uploads={}, already_done=set(), declaration_accepted=False)
    assert first.step is Step.FILL

    second = plan_next(page, values={"name": "N"}, uploads={}, already_done={"name"}, declaration_accepted=False)
    assert second.step is Step.CLICK, "a filled field was planned again"


# ===========================================================================
# THE OTHER UNBOUNDED PATHS
# ===========================================================================
def test_a_consequential_action_is_never_repeated_even_if_the_page_keeps_offering_it():
    """Submit is the one action whose repetition cannot be undone."""

    class FailingSubmit:
        name = "fail-submit"

        def __init__(self):
            self.actions = []

        def launch(self, *, profile_dir, headless=True):
            pass

        def close(self):
            pass

        def observe(self):
            return PageState(url="https://p/a", fields={}, controls=["Submit application"])

        def act(self, step, target, value=None):
            self.actions.append((step, target))
            from agent.browser_runtime import Failure

            return ActionResult(ok=False, failure=Failure.TRANSIENT_NETWORK, detail="reset")

        def screenshot(self):
            return ""

    provider = FailingSubmit()
    report = BrowserRuntime(provider, policy=RetryPolicy(max_total_actions=60)).run(
        task(), values={}, uploads={}, submission_authorised=True
    )
    submits = [a for a in provider.actions if a[0] is Step.SUBMIT]
    assert len(submits) == 1, f"SUBMIT was attempted {len(submits)} times"
    assert report.status == "UNCERTAIN"
    assert report.outcome_certain is False


def test_the_session_is_released_after_a_loop_is_refused():
    """A BLOCKED run must still give the browser back - the next organisation's job needs it."""
    provider = StaticProvider()
    BrowserRuntime(provider).run(task(), values={}, uploads={})
    assert provider.closed is True


def test_consequential_actions_remain_a_one_item_set():
    """If this grows, the no-repeat rule silently stops covering the new member."""
    assert CONSEQUENTIAL == frozenset({Step.SUBMIT})
