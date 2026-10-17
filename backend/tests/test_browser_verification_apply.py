"""Applying a verification code: the last untested link in the mission.

WHY THIS FILE EXISTS. The browser worker's whole purpose is to complete a portal signup, and the step
that finishes it - entering the verification code that arrives by email - was covered by **no test at
all**. Every unit test stopped at "the code arrived in the mailbox"; nothing checked that the worker
then puts it into the right field.

That gap was tolerable while mail could not arrive (the Cloudflare catch-all is not enabled yet). It
would stop being tolerable the moment it could, which is exactly when a defect here would be found by
a customer instead of by the suite.

WHAT IS PINNED:
  * `_first_text_input` never guesses. If it cannot identify an empty, visible, enabled text field it
    returns "" - entering a one-time code into an unrelated or hidden input is worse than stopping.
  * `_act_fill` acts on the element that was OBSERVED, and only falls back to a `[name=...]` guess
    when the observed one is gone. The UN's B2C form is why: the name selector did not resolve, and
    the only symptom was a Next button that stayed disabled.
  * a failed ref fill falls back rather than aborting the run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.browser_runtime import Failure, Step  # noqa: E402
from tools.browser_worker import _act_fill, _first_text_input  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes: enough of a page for the questions these helpers actually ask
# ---------------------------------------------------------------------------
class FakeLocator:
    def __init__(self, *, count=1, raises=None):
        self._count = count
        self._raises = raises
        self.filled: list[str] = []

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def fill(self, value, timeout=None):
        if self._raises:
            raise self._raises
        self.filled.append(value)


class FakePage:
    def __init__(self, *, name="", evaluate_raises=None, locator=None):
        self._name = name
        self._evaluate_raises = evaluate_raises
        self._locator = locator or FakeLocator()
        self.selectors: list[str] = []

    def evaluate(self, script):
        if self._evaluate_raises:
            raise self._evaluate_raises
        return self._name

    def locator(self, selector):
        self.selectors.append(selector)
        return self._locator


class FakeProvider:
    """Records what the worker asked the page to do."""

    def __init__(self, page=None, *, act_raises=None, act_ok=True):
        self._page = page or FakePage()
        self._act_raises = act_raises
        self._act_ok = act_ok
        self.act_calls: list[tuple] = []
        self.observed = 0

    def act(self, step, *args):
        self.act_calls.append((step, *args))
        if self._act_raises:
            raise self._act_raises
        from agent.browser_runtime import ActionResult

        return ActionResult(ok=self._act_ok)

    def observe(self):
        self.observed += 1
        return {"text": "page", "controls": []}


# ---------------------------------------------------------------------------
# Finding the field: never guess
# ---------------------------------------------------------------------------
def test_the_first_empty_text_input_is_returned_by_name():
    provider = FakeProvider(FakePage(name="verificationCode"))
    assert _first_text_input(provider) == "verificationCode"


def test_an_empty_answer_is_returned_as_empty_not_guessed():
    """The page said there is no usable field. Inventing one would type a one-time code into
    whatever happened to be first."""
    provider = FakeProvider(FakePage(name=""))
    assert _first_text_input(provider) == ""


def test_a_page_that_cannot_be_read_yields_empty():
    """A raising evaluate must not propagate: a code that cannot be placed is a reason to stop the
    step, not to abort the run."""
    provider = FakeProvider(FakePage(evaluate_raises=RuntimeError("detached frame")))
    assert _first_text_input(provider) == ""


# ---------------------------------------------------------------------------
# Filling: the observed element first
# ---------------------------------------------------------------------------
def test_the_observed_element_is_filled_when_the_ref_resolves():
    locator = FakeLocator(count=1)
    provider = FakeProvider(FakePage(locator=locator))

    result = _act_fill(provider, "verificationCode", "481920", ref="f3")

    assert result.ok is True
    assert locator.filled == ["481920"], "the code must go into the element that was seen"
    assert provider.act_calls == [], "a resolved ref must not fall back to a name selector"
    assert provider._page.selectors == ["[data-granada-fill='f3']"]
    assert provider.observed == 1


def test_the_name_selector_is_the_fallback_when_the_ref_is_gone():
    """The ref belongs to an observation that may be stale by the time the code arrives; a run that
    died here would be one field from finishing."""
    locator = FakeLocator(count=0)
    provider = FakeProvider(FakePage(locator=locator))

    result = _act_fill(provider, "verificationCode", "481920", ref="f3")

    assert result.ok is True
    assert locator.filled == []
    assert provider.act_calls == [(Step.FILL, "verificationCode", "481920")]


def test_a_failed_ref_fill_falls_back_instead_of_aborting():
    locator = FakeLocator(count=1, raises=RuntimeError("element is not editable"))
    provider = FakeProvider(FakePage(locator=locator))

    result = _act_fill(provider, "verificationCode", "481920", ref="f3")

    assert locator.filled == []
    assert provider.act_calls == [(Step.FILL, "verificationCode", "481920")]
    assert result.ok is True


def test_no_ref_goes_straight_to_the_provider():
    provider = FakeProvider()
    result = _act_fill(provider, "email", "ngo@example.org")
    assert provider.act_calls == [(Step.FILL, "email", "ngo@example.org")]
    assert result.ok is True


def test_a_total_failure_is_a_failure_result_not_an_exception():
    """The caller inspects `result.ok`; a raised exception here would strand the workflow rather than
    closing it honestly."""
    provider = FakeProvider(act_raises=RuntimeError("no such field"))

    result = _act_fill(provider, "verificationCode", "481920")

    assert result.ok is False
    assert result.failure == Failure.UNKNOWN
    assert "no such field" in (result.detail or "")


# ---------------------------------------------------------------------------
# The call sites: a helper nothing calls is the defect this codebase keeps hitting
# ---------------------------------------------------------------------------
def _worker_source() -> str:
    return (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")


def test_the_verification_apply_path_actually_calls_the_helpers():
    """`GranadaAgentService.provision()` was written, documented as "called from registration", and
    called by nothing but tests for months - so every real organisation had no agent. A helper is
    only real if the run path reaches it."""
    source = _worker_source()

    assert "_first_text_input(provider)" in source, (
        "the worker never looks for the code field, so an arriving code could not be entered"
    )
    assert "_act_fill(provider" in source, (
        "the worker never fills a field through the observed-element path"
    )

    # Both must be reached from the RUN PATH, not merely defined. `_run_goal` is the function that
    # executes a step, and it appears earlier in the file than the helpers it calls.
    run_goal = source.index("def _run_goal")
    assert source.index("_first_text_input(provider)") > run_goal, (
        "the code-field lookup is not inside the run path"
    )
    assert source.index("_act_fill(provider") > run_goal, (
        "the fill is not inside the run path"
    )


def test_a_verification_code_is_registered_with_the_scrubber_before_it_is_typed():
    """A code echoed back by a rejecting form would otherwise be written into the job outcome, the
    activity record and the trace - all of them readable."""
    source = _worker_source()

    scrub = source.index('_RUN_SECRETS.add(str(result["code"]))')
    typed = source.index('provider.act(Step.FILL, code_field, str(result["code"]))')
    assert scrub < typed, (
        "the code must be registered as a secret BEFORE it can be typed, or a rejection that echoes "
        "it leaks the value into the record"
    )


def test_the_code_is_never_put_in_the_trace():
    """The trace records that a verification arrived and what was done with it, never the value."""
    source = _worker_source()
    # Everything between the apply block and its end must not reference the code value in a trace
    # entry. A blunt but effective check: the trace never mentions the literal key.
    assert '"code": str(result["code"])' not in source

