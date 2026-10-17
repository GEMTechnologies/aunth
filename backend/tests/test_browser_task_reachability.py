"""Is the agent's most important capability actually reachable from production code?

A REGISTERED HANDLER IS NOT A CAPABILITY. `WORKFLOW_BROWSER_TASK` is defined once in
`agent/workflow_engine.py` and created by NOTHING: not the API, not fleet discovery, not the application
flow. The handler is registered against the COMPLIANCE specialist, the worker exists, the agent's
settings enable it, and the workflow type can never occur. Measured on production: `browser_workflows=0`,
on an agent whose settings contain `"browser_execution_enabled": true` and a real UN-portal goal.

So the platform can browse the UN portal and has never been asked to. Every browser run in this project's
history was a human invoking the worker directly - never the product.

This is `GranadaAgentService.provision()` again, one layer up and at the layer that matters most: a
documented, tested, never-called entry point.

WHY AN XFAIL AND NOT A SKIP. A skip is invisible. This test encodes the gap in the suite, with the exact
remedy, and `strict=True` means the moment the enqueue path lands it turns into a FAILURE until someone
deletes the marker - so the fix cannot quietly leave a stale "known gap" behind.

WHAT THE FIX HAS TO DO (recorded here because finding it cost a whole round):
  * `AgentWake.schedule(db, agent=..., workflow_type="browser_task", specialist_key="COMPLIANCE", ...)`
    - COMPLIANCE owns both `application_assemble` and `browser_task` in `agent/specialists.py`
  * reached from an API route the UI can call, because the frontend has no reference to the portal at all
  * the subject type/id contract must be settled first: the handler builds a task from the workflow, and a
    wrong subject dispatches a real browser run rather than failing loudly
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

#: Files that constitute PRODUCTION code for this question. Tests are excluded on purpose: a test that
#: creates the workflow proves the handler works, not that anything can ask for it.
PRODUCTION = list((BACKEND / "agent").rglob("*.py")) + [
    p for p in BACKEND.glob("*.py") if p.name not in {"conftest.py"}
]


def _creation_sites() -> list[str]:
    """Every place a browser_task workflow is created, excluding the definition itself."""
    found = []
    pattern = re.compile(
        r"""workflow_type\s*=\s*WORKFLOW_BROWSER_TASK|workflow_type\s*=\s*["']browser_task["']"""
    )
    for path in PRODUCTION:
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(source):
            # The constant's own definition is not a creation site.
            line = source[max(0, match.start() - 120) : match.start()].splitlines()[-1]
            if line.strip().startswith("WORKFLOW_BROWSER_TASK"):
                continue
            found.append(f"{path.name}:{source[: match.start()].count(chr(10)) + 1}")
    return found


def test_production_code_can_create_a_browser_task():
    """WAS AN XFAIL FOR ONE ROUND. It failed, by design, until `POST /agent/portal/registration` was
    added - the trigger that did not exist. `strict=True` meant it would turn into a FAILURE the moment
    the gap closed, which is why this marker is gone rather than left behind as a stale "known gap".
    """
    assert _creation_sites(), (
        "no production code creates a browser_task workflow: the capability is registered, configured "
        "and unreachable, and the mission can never start"
    )


def test_the_route_that_creates_it_exists_and_is_reachable_http():
    """The creation site must be a ROUTE, not merely a helper: the gap was that no client could ask."""
    import agent_api

    paths = {route.path for route in agent_api.router.routes}
    assert "/agent/portal/registration" in paths

    source = (BACKEND / "agent_api.py").read_text(encoding="utf-8")
    assert 'workflow_type="browser_task"' in source
    # The specialist is not a free choice: naming one that does not advertise this work type produces
    # UnknownSpecialist at execution time, not at request time.
    assert 'specialist_key="COMPLIANCE"' in source


def test_the_handler_is_still_registered_under_compliance():
    """The counterpart: if the mapping ever moves, the remedy recorded above becomes wrong."""
    source = (BACKEND / "agent" / "specialists.py").read_text(encoding="utf-8")
    assert '"browser_task": "_handle_browser_task"' in source
    assert '"COMPLIANCE"' in source


def test_submission_authority_is_asked_rather_than_assumed():
    """The flag used to be hard-coded False while a fully-written authority module was imported by
    nothing - so the platform could prepare, approve and authorise, and still never complete, because
    nobody asked the component that decides.

    This is a reachability check, not a behaviour check: the behaviour is covered by
    test_submission and by the module's own fail-closed evaluation. What it prevents is the specific
    regression of passing a literal False again.
    """
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")

    assert "submission_authority.evaluate(" in source, (
        "the browser handler no longer asks the authority module whether it may submit"
    )
    assert "submission_authorised=False," not in source, (
        "submission authority is hard-coded off again; no authorisation could ever take effect"
    )
    # And the decision must actually reach the invocation.
    assert "submission_authorised=authority.allowed" in source
