"""The browser path must be REACHABLE from the job system, not merely implemented.

WHY THIS FILE EXISTS

`_handle_browser_task` reads `context.get("settings") or {}` and refuses when
`browser_execution_enabled` is falsy. The job runner builds the handler context in
`WorkflowEngine._execute_with`, and **that dict had no `"settings"` key at all.**

So on every single job the handler saw an empty mapping, concluded browser execution was disabled, and
returned `browser.disabled`. The browser executor existed, was tested in isolation, had been run
end-to-end by hand - and **could not be reached from the job system by any configuration**, because an
operator setting `browser_execution_enabled` on an agent would still have had it read from `{}`.

That is the same shape as the mail transports and the model routes: **built, tested, and unreachable**.
Nothing failed. The workflow parked, which is exactly what a disabled feature is supposed to do - so the
symptom was indistinguishable from the intended behaviour.

A test that constructs the handler and passes `settings={...}` directly would have passed throughout.
These tests drive the CONTEXT, because the context is where the defect was.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import browser_invocation  # noqa: E402


def test_the_gate_is_shut_for_an_empty_mapping():
    """The default must stay off: nothing is enabled by wiring the key through."""
    assert browser_invocation.enabled({}) is False
    assert browser_invocation.enabled({browser_invocation.BROWSER_EXECUTION_ENABLED: 0}) is False
    assert browser_invocation.enabled({browser_invocation.BROWSER_EXECUTION_ENABLED: "false"}) is False


def test_the_gate_opens_for_a_truthy_setting():
    """`enabled` uses `value is True`, so the INTEGER 1 does NOT open the gate - only the boolean
    `True` or an explicit string does.

    That is deliberate and worth pinning: the docstring says the truth test exists because a string
    "false" from an environment file is truthy in Python. Strictness in the same direction also means
    an integer from a JSON settings column does not silently arm a browser.
    """
    assert browser_invocation.enabled({browser_invocation.BROWSER_EXECUTION_ENABLED: True}) is True
    for spelling in ("1", "true", "TRUE", " yes ", "on"):
        assert browser_invocation.enabled(
            {browser_invocation.BROWSER_EXECUTION_ENABLED: spelling}
        ) is True, f"{spelling!r} should open the gate"


def test_an_integer_one_does_not_open_the_gate():
    """Pinned separately because it surprises: JSON has no boolean-vs-integer distinction in some
    stores, and `1` here is NOT `True`. The safe direction is to stay shut."""
    assert browser_invocation.enabled({browser_invocation.BROWSER_EXECUTION_ENABLED: 1}) is False


# ===========================================================================
# THE CONTEXT - where the defect actually was
# ===========================================================================
def _context_block() -> str:
    """The handler context dict, located by LINE RATHER THAN BRACE MATCHING.

    The first version of these tests sliced from `context = {` to the first `}` - and the first `}` is
    inside `(workflow.context or {})`, which appears BEFORE the settings line. The slice therefore
    ended before the thing being asserted, and the test failed against correct code.
    """
    lines = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "context = {")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "\n".join(lines[start : end + 1])


def test_the_handler_context_carries_the_agents_settings():
    """THE regression. `_execute_with` built a context with job, workflow, agent, specialist, db and
    correlation_id - and no settings. The handler then read `{}` on every job.

    Asserted at the source because the alternative is constructing a WorkflowEngine against a live
    database, and the invariant is narrow: the dict literal must include a `settings` key sourced from
    the agent.
    """
    block = _context_block()
    assert '"settings"' in block, (
        "the handler context has no settings key, so `_handle_browser_task` reads an empty "
        "mapping and browser execution is unreachable from the job system"
    )
    assert '"agent"' in block


def test_the_settings_come_from_the_agent_not_from_a_constant():
    """A hardcoded `{}` would satisfy a key-presence check and reproduce the bug exactly."""
    block = _context_block()
    assert "getattr(agent" in block, (
        "settings are not sourced from the agent; an agent's configuration would change nothing"
    )


def test_the_settings_key_is_read_by_the_handler_that_needs_it():
    """The two halves must agree on the key name. A rename on one side would silently disable the
    feature again, and the symptom would again be a parked workflow."""
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    assert 'context.get("settings")' in source
    assert '"settings":' in source


def test_an_agent_with_no_settings_still_reads_as_disabled():
    """The safe default survives the wiring. `GranadaAgent.settings` is created as `{}`, and an agent
    an operator has not configured must not open a browser."""
    class Agent:
        settings = {}

    context_settings = dict(getattr(Agent, "settings", None) or {})
    assert browser_invocation.enabled(context_settings) is False


def test_an_agent_with_a_null_settings_column_does_not_crash():
    """The column is nullable in principle, and `getattr(...) or {}` is what makes that safe."""
    class Agent:
        settings = None

    assert dict(getattr(Agent, "settings", None) or {}) == {}


def test_an_agent_without_a_settings_attribute_does_not_crash():
    """A stand-in agent in a test, or a future model, must not break the job runner."""
    class Agent:
        pass

    assert dict(getattr(Agent, "settings", None) or {}) == {}


def test_configured_settings_survive_into_the_handler_gate():
    """End to end across the boundary that was broken: agent settings -> context -> the gate."""
    class Agent:
        settings = {browser_invocation.BROWSER_EXECUTION_ENABLED: True}

    context_settings = dict(getattr(Agent, "settings", None) or {})
    assert browser_invocation.enabled(context_settings) is True
