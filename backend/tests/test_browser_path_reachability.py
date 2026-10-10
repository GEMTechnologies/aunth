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


# ===========================================================================
# THE DESTINATION - the field the whole path was missing
# ===========================================================================
def _task(**overrides):
    from agent.browser_boundary import ActionScope, BrowserTask

    fields = dict(
        task_id="task-1",
        org_id="org-1",
        package_id="pkg-1",
        workflow_id=None,
        job_id=None,
        package_fingerprint="fp-1",
        action_scope=ActionScope(portal_name="fixture", allowed_hosts=("127.0.0.1",)),
    )
    fields.update(overrides)
    return BrowserTask(**fields)


def test_a_task_can_carry_a_destination_at_all():
    """`validate_task` screened `getattr(task, "target_url", "")` and `as_dict()` omitted the key,
    while the worker reads `payload["target_url"]` and refuses to report completion without it. The
    field simply did not exist, so every job-path invocation had nowhere to go - and the worker
    driven by hand with a literal payload worked, which is precisely why nothing caught it."""
    from agent.browser_boundary import BrowserTask

    assert "target_url" in BrowserTask.__dataclass_fields__, (
        "BrowserTask still has no target_url field, so no job can tell the worker where to open"
    )


def test_the_serialised_payload_carries_the_key_the_worker_reads():
    """The two halves are in different files and were never checked against each other."""
    payload = _task(target_url="https://funder.example/apply").as_dict()
    assert payload["target_url"] == "https://funder.example/apply"

    worker = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    assert 'payload.get("target_url")' in worker, (
        "the worker stopped reading target_url; the payload key is now unverified on the other side"
    )


def test_build_task_carries_the_target_from_the_package():
    """The package is where the funder's portal URL is recorded, and `build_task` already had it."""
    source = (BACKEND / "agent" / "browser_boundary.py").read_text(encoding="utf-8")
    build = source.split("def build_task", 1)[1].split("def describe_integration", 1)[0]
    assert "target_url=" in build, "build_task does not pass the package's target_url into the task"


def test_validate_task_screens_the_target_now_that_the_field_exists():
    """The check was written and then made vacuous by `getattr(task, "target_url", "")` defaulting to
    empty, because the field did not exist. With the field present it can actually refuse something.

    A LITERAL link-local address is refused even with `resolve=False`, so this is a real refusal and
    not a DNS-dependent one.
    """
    from agent.browser_boundary import ActionScope, BrowserTaskRefused, validate_task

    allowed = ActionScope(portal_name="funder", allowed_hosts=("funder.example",))

    # A permitted destination is permitted.
    validate_task(
        _task(action_scope=allowed, target_url="https://funder.example/apply"),
        org_document_ids=set(),
    )

    # A permitted HOST with a metadata-endpoint target is refused - the allow-list names the host,
    # not the address the browser is actually sent to.
    with pytest.raises(BrowserTaskRefused):
        validate_task(
            _task(action_scope=allowed, target_url="http://169.254.169.254/latest/meta-data/"),
            org_document_ids=set(),
        )


def test_a_loopback_HOST_is_refused_unless_the_task_opts_in():
    """The other half of the same screen: `screen_hosts` runs over `allowed_hosts` at build time."""
    from agent.browser_boundary import ActionScope, BrowserTaskRefused, validate_task

    with pytest.raises(BrowserTaskRefused):
        validate_task(
            _task(action_scope=ActionScope(portal_name="fixture", allowed_hosts=("127.0.0.1",))),
            org_document_ids=set(),
        )

    # The same task, with the opt-in the scope now actually receives from configuration.
    validate_task(
        _task(
            action_scope=ActionScope(
                portal_name="fixture", allowed_hosts=("127.0.0.1",), allow_loopback=True
            ),
            target_url="http://127.0.0.1:8099/",
        ),
        org_document_ids=set(),
    )


# ===========================================================================
# THE LOOPBACK OPT-IN - reachable from the job path at last
# ===========================================================================
def test_the_scope_does_not_permit_loopback_by_default():
    from agent.workflow_engine import _browser_scope_for

    scope = _browser_scope_for(None, {"browser_allowed_hosts": ["127.0.0.1"]})
    assert scope.allow_loopback is False, (
        "a production task must not inherit loopback permission by being built the same way as a "
        "fixture"
    )


def test_the_scope_honours_an_explicit_loopback_opt_in():
    """`ActionScope.allow_loopback` existed, `validate_task` read it, and the ONLY place production
    builds a scope never set it - so the controlled fixture could not be driven through the job
    system at all. That is the same defect already fixed one layer down in `browser_worker`, which
    dropped the value in transit: a refusal naming a switch nothing can set."""
    from agent.workflow_engine import _browser_scope_for

    scope = _browser_scope_for(None, {
        "browser_allowed_hosts": ["127.0.0.1"],
        "browser_allow_loopback": True,
    })
    assert scope.allow_loopback is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", None])
def test_a_recognised_false_word_does_not_permit_loopback(value):
    """`bool("false")` is True. A settings file that says "false" must not open loopback."""
    from agent.workflow_engine import _browser_scope_for

    scope = _browser_scope_for(None, {
        "browser_allowed_hosts": ["127.0.0.1"],
        "browser_allow_loopback": value,
    })
    assert scope.allow_loopback is False


def test_the_opt_in_is_collected_from_settings_not_from_the_package():
    """It is per-task configuration. Reading it off the package would let package data grant itself
    permission to reach the internal network."""
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    scope_fn = source.split("def _browser_scope_for", 1)[1].split("\n\n\ndef ", 1)[0]
    assert "browser_allow_loopback" in scope_fn
    assert "_truthy(" in scope_fn, "a bare truth test would turn 'false' into True"


def test_the_scope_ROUND_TRIPS_through_serialisation():
    """THE BOUNDARY WHERE THE SWITCH KEPT DISAPPEARING.

    `as_dict()` is the only thing the worker sees - it rebuilds an `ActionScope` from it and re-checks
    the target. `allow_loopback` was omitted, so the opt-in survived construction, survived
    `validate_task` in-process, and died at serialisation: the worker read `None is True` -> False and
    refused the controlled fixture as an SSRF risk. Asserting field-by-field would have missed it
    again; this asserts the WHOLE object survives, so a future field cannot join the list silently.
    """
    from agent.browser_boundary import ActionScope

    original = ActionScope(
        portal_name="fixture",
        allowed_hosts=("127.0.0.1",),
        allowed_path_prefixes=("/apply",),
        max_steps=42,
        allow_loopback=True,
    )
    emitted = original.as_dict()

    # Rebuilt exactly as `browser_worker` rebuilds it. JSON has no tuples, so the emitted lists are
    # converted back - that asymmetry is real and is the worker's own `tuple(...)` call.
    rebuilt = ActionScope(
        portal_name=emitted["portal_name"],
        allowed_hosts=tuple(emitted["allowed_hosts"]),
        allowed_path_prefixes=tuple(emitted["allowed_path_prefixes"]),
        max_steps=emitted["max_steps"],
        allow_loopback=emitted["allow_loopback"],
    )
    assert rebuilt == original, "the scope does not survive its own serialisation"


def test_the_worker_rebuilds_the_scope_from_the_serialised_form():
    """The other side of the boundary: whatever `as_dict()` emits is what the worker reads."""
    from agent.browser_boundary import ActionScope

    payload_scope = ActionScope(
        portal_name="fixture", allowed_hosts=("127.0.0.1",), allow_loopback=True
    ).as_dict()
    worker = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    assert 'scope_payload.get("allow_loopback")' in worker, (
        "the worker stopped reading the opt-in; the serialised key is now unverified on that side"
    )
    assert payload_scope["allow_loopback"] is True
