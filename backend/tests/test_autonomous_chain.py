"""The pipeline must advance itself. A chain that needs a human to schedule each step is not autonomous.

THE GAP. `_handle_donor_research` ended with:

    # STOP HERE for this phase. The proposal is not generated and nothing is
    # submitted: Phase 6c proves autonomous INTERNAL work.

Honest when no Document Agent existed. One does now, and research already knows which documents the
listing requires - `required_documents` is in its own structured_data - so stopping there left the
fleet idle with the next step sitting in the previous step's output.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.workflow_engine import (  # noqa: E402
    WORKFLOW_DOCUMENT,
    WORKFLOW_QUALIFY,
    WORKFLOW_RESEARCH,
)

SOURCE = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")


def _enqueued_by(handler_name: str) -> list[str]:
    """The WORKFLOW_* a handler enqueues, read from the AST.

    From the tree rather than the text: the handler bodies discuss the workflow types at length in
    comments, and a substring search would be satisfied by a comment rather than by a call.
    """
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == handler_name:
            found: list[str] = []
            for sub in ast.walk(node):
                # Look for the "enqueue" dict literal and collect WORKFLOW_* names inside it.
                if isinstance(sub, ast.Dict):
                    for key, value in zip(sub.keys, sub.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "enqueue"
                            and isinstance(value, ast.Dict)
                        ):
                            for inner in ast.walk(value):
                                if isinstance(inner, ast.Name) and inner.id.startswith("WORKFLOW_"):
                                    found.append(inner.id)
                if isinstance(sub, ast.Name) and sub.id.startswith("WORKFLOW_"):
                    found.append(sub.id)
            # De-duplicate, keeping source order.
            seen: list[str] = []
            for name in found:
                if name not in seen:
                    seen.append(name)
            return seen
    raise AssertionError(f"{handler_name} not found - has the engine been restructured?")


def test_match_hands_off_to_qualify():
    assert "WORKFLOW_QUALIFY" in _enqueued_by("_handle_match")


def test_qualify_hands_off_to_research():
    assert "WORKFLOW_RESEARCH" in _enqueued_by("_handle_qualify")


def test_research_hands_off_to_DOCUMENTS():
    """THE new link, and the one that makes the pipeline autonomous.

    Without it the chain ended at research and the fleet went idle with the required documents
    already known and nowhere to go.
    """
    assert "WORKFLOW_DOCUMENT" in _enqueued_by("_handle_donor_research"), (
        "research does not hand off to document generation, so the pipeline stops and needs a human "
        "to schedule the next step"
    )


def test_the_chain_is_a_chain_not_a_star():
    """Each step hands off to exactly the ONE that follows it.

    A handler that enqueued several successors would run work in parallel that is ordered - the
    application workspace is a state machine, and documents assembled before research would not know
    which documents the listing asks for.
    """
    assert _enqueued_by("_handle_match") == ["WORKFLOW_QUALIFY"]
    assert _enqueued_by("_handle_qualify") == ["WORKFLOW_RESEARCH"]
    assert _enqueued_by("_handle_donor_research") == ["WORKFLOW_DOCUMENT"]


def test_documents_is_the_current_end_and_submission_is_absent():
    """Recording where the chain STOPS, so the boundary is deliberate rather than discovered.

    Submission stays behind the approval gate and is not implemented; a test that asserts its absence
    means implementing it has to be a decision.
    """
    from agent import workflow_engine as engine

    document_handlers = [
        name
        for name in dir(engine)
        if name.startswith("_handle_") and name.endswith("submit")
    ]
    assert not document_handlers, (
        f"a submission handler now exists ({document_handlers}) - that is a deliberate change and "
        "this test should be updated to assert its approval gate, not deleted"
    )
    assert WORKFLOW_DOCUMENT == "document_generate"
