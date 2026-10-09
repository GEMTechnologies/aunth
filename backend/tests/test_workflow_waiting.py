"""A parked workflow must not be re-dispatched on every sweep.

THE DEFECT, measured on the VPS on 2026-10-09:

    SUCCEEDED = 1146 jobs, from 2 workflows
    attempt values seen: 1            <- NOT retries; all new jobs
    distinct idempotency keys = 1146 of 1146
    last 60 seconds = 8               <- still accruing

`due_workflows` selects `state IN (PENDING, WAITING) AND next_run_at <= now`. The branch that applied
a handler's `next_state` handled `RUNNING` and set `next_run_at = now`, but had NO BRANCH FOR WAITING -
so a workflow parked on missing organisation information kept whatever `next_run_at` it already had,
which was in the past the moment it was written. The next sweep found it due, dispatched another job,
the handler parked it again, and the loop ran for as long as the platform did.

NOTHING FAILED, which is why it went unnoticed: every job SUCCEEDED, no exception was logged, and the
only symptom was a table growing by eight rows a minute.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.workflow_engine import WAITING_RETRY_BACKOFF  # noqa: E402


def _next_run_at_assignment() -> ast.If:
    """The `if activity.get("next_run_at")` chain, from the source."""
    tree = ast.parse((BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "next_run_at" in test and "activity.get" in test:
                return node
    raise AssertionError("the next_run_at chain was not found - has the engine been restructured?")


def _branches(node: ast.If) -> list[str]:
    """Every `next_state == X` compared anywhere in the chain, in order."""
    found: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare):
            text = ast.unparse(sub)
            if "next_state" in text and "AgentWorkflow" in text:
                found.append(text.rsplit(".", 1)[-1])
    return found


def test_WAITING_is_handled_where_next_run_at_is_decided():
    """THE regression guard.

    If `WAITING` is absent from this chain, every workflow that parks on missing information becomes
    immortal and the fleet dispatches a job for it on every single sweep.
    """
    states = _branches(_next_run_at_assignment())
    assert "WAITING" in states, (
        "the next_run_at chain has no WAITING branch, so a parked workflow keeps a past next_run_at "
        "and is re-dispatched on every sweep - 1,146 jobs from 2 workflows was the measured result"
    )
    assert "RUNNING" in states, "the RUNNING branch went missing"


def test_WAITING_pushes_next_run_at_forward_not_backwards():
    """The branch must move the time FORWARD. Setting it to `now` would leave the row due again
    immediately, which is the same bug wearing a different hat."""
    node = _next_run_at_assignment()
    for sub in ast.walk(node):
        if isinstance(sub, ast.If):
            text = ast.unparse(sub)
            if "WAITING" in text:
                assert "WAITING_RETRY_BACKOFF" in text, (
                    "the WAITING branch does not apply a backoff, so the workflow stays due"
                )
                assert "datetime.now" in text, "the backoff is not relative to now"
                return
    raise AssertionError("no WAITING branch found")


def test_the_backoff_is_long_enough_to_matter():
    """The blocker is a HUMAN supplying facts. Seconds would be a spin; the value should be hours."""
    assert WAITING_RETRY_BACKOFF.total_seconds() >= 3600, (
        f"a {WAITING_RETRY_BACKOFF} backoff is a busy loop dressed up as patience"
    )
