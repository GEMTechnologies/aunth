"""The ADR-0011 dispatcher cutover: the narrow claim path must actually RUN.

WHY THIS FILE EXISTS
--------------------
The cutover was added to `FleetDispatcher.due_workflows` behind `USE_NARROW_CLAIM`, defaulting False so
that no existing behaviour changes. That is the right shape for a production change - and it created a
specific hazard:

    a path that is never taken is a path that is never tested

It bit immediately. The first version called `text(...)` and **`text` was not imported** in that
module. The whole fleet suite passed, because the flag is False and the broken line never executed. A
`NameError` was sitting in production-ready code with a green suite over it.

So these tests flip the flag ON and assert the path runs, rather than asserting it exists.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


# ===========================================================================
# THE FLAG
# ===========================================================================
def test_the_narrow_claim_defaults_to_OFF():
    """Additive by design. Switching the default in the same commit that introduces the option would
    make a regression indistinguishable from the intended change."""
    from agent.workflow_engine import FleetDispatcher

    assert FleetDispatcher.USE_NARROW_CLAIM is False, (
        "the cutover is on by default; it must be an explicit, reversible configuration decision"
    )


def test_the_flag_is_a_class_attribute_not_a_local():
    """It has to be settable by configuration and visible to a test without constructing the class
    against a live database."""
    from agent.workflow_engine import FleetDispatcher

    assert isinstance(FleetDispatcher.__dict__.get("USE_NARROW_CLAIM"), bool)


def test_both_paths_exist_in_the_source():
    """A static check that the narrow path was not silently deleted while the flag remained - which
    would leave a switch that does nothing."""
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    assert "USE_NARROW_CLAIM" in source
    assert "fleet_due_workflow_ids" in source, "the narrow path is gone but the flag remains"


def test_sqlalchemy_text_is_imported():
    """THE regression. `text()` was used in the narrow path and never imported; every test passed
    because the path was off. This asserts the name is available at module scope, which is exactly
    what was missing."""
    import agent.workflow_engine as engine

    assert hasattr(engine, "text"), f"{engine.__name__} uses text() but does not import it"


# ===========================================================================
# THE ENABLED PATH ACTUALLY RUNS
# ===========================================================================
class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _StubDB:
    """Records the SQL it was asked to run and returns canned rows.

    The point is not to simulate a database faithfully - it is to prove the narrow branch is REACHED
    and that its statements are well-formed enough to build. A stub that never gets called fails the
    test, which is the outcome that matters.
    """

    def __init__(self, ids):
        self._ids = ids
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        rendered = str(statement)
        self.statements.append(rendered)
        if "fleet_due_workflow_ids" in rendered:
            return _StubResult([(i,) for i in self._ids])
        return _StubResult([])

    def rollback(self):
        pass


def _dispatcher(db):
    from agent.workflow_engine import FleetDispatcher

    d = FleetDispatcher(db, batch_size=200, per_agent_limit=25)
    return d


def test_enabling_the_flag_reaches_the_function(monkeypatch):
    """Flip it ON and require the function to be called. If the branch were unreachable - a typo in the
    attribute name, a shadowing local - this fails."""
    from agent.workflow_engine import FleetDispatcher

    db = _StubDB([])
    monkeypatch.setattr(FleetDispatcher, "USE_NARROW_CLAIM", True)

    dispatcher = _dispatcher(db)
    dispatcher.due_workflows(now=datetime.now(timezone.utc))

    assert any("fleet_due_workflow_ids" in s for s in db.statements), (
        "the narrow claim was enabled but the function was never called - the branch is unreachable"
    )


def test_enabling_the_flag_does_not_run_the_old_query(monkeypatch):
    """The cutover must actually replace the BYPASSRLS path, not run both. Running both would keep the
    privileged read in the system while appearing narrowed."""
    from agent.workflow_engine import FleetDispatcher

    db = _StubDB([])
    monkeypatch.setattr(FleetDispatcher, "USE_NARROW_CLAIM", True)
    _dispatcher(db).due_workflows(now=datetime.now(timezone.utc))

    joined = " ".join(db.statements)
    assert "row_number" not in joined, (
        "the old in-Python window query still ran with the flag on; the cutover is incomplete"
    )


def test_the_default_path_does_not_call_the_function(monkeypatch):
    """And the converse: with the flag off, the function must not be reached. Otherwise the 'additive'
    claim is false and the default path has already changed."""
    from agent.workflow_engine import FleetDispatcher

    db = _StubDB([])
    monkeypatch.setattr(FleetDispatcher, "USE_NARROW_CLAIM", False)
    try:
        _dispatcher(db).due_workflows(now=datetime.now(timezone.utc))
    except Exception:
        # The default path builds a real ORM query; a stub session may not satisfy it. The assertion
        # that matters is that the FUNCTION was not called.
        pass

    assert not any("fleet_due_workflow_ids" in s for s in db.statements), (
        "the function ran with the cutover OFF - the change is not additive"
    )


def test_an_empty_candidate_set_returns_early(monkeypatch):
    """No ids means no re-select. A needless `IN ()` is a wasted round trip and, on some drivers, a
    syntax error."""
    from agent.workflow_engine import FleetDispatcher

    db = _StubDB([])
    monkeypatch.setattr(FleetDispatcher, "USE_NARROW_CLAIM", True)
    rows = _dispatcher(db).due_workflows(now=datetime.now(timezone.utc))

    assert rows == []
    assert len(db.statements) == 1, "an empty candidate set still issued a second query"
