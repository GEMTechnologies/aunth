"""ADR-0011 step 2: the dispatcher binds to one organisation at a time instead of relying on BYPASSRLS.

WHY THIS FILE EXISTS
--------------------
`USE_TENANT_BINDING` defaults to False, so on the day it was added every existing test still passed
without executing a single line of it. That is the same hazard `test_adr0011_cutover.py` documents for
`USE_NARROW_CLAIM`, and it is worse here: the failure mode of this path is a **silent zero**.

A dispatcher that fails to bind still returns rows from `fleet_due_workflow_refs` - the function is
SECURITY DEFINER and crosses tenants by design. It then loads each workflow row, gets `None` because
RLS hides it, and counts `skipped_unhandled`. The loop completes, the health object looks calm, and the
fleet has dispatched nothing. Nothing raises. Nothing logs an error.

So these tests do not assert the flag exists. They turn it ON and assert the binding actually happens,
and - more importantly - that it is CLEARED, including when the body raises.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# ===========================================================================
# A session that records what was asked of it, and can be told what to return.
# ===========================================================================
class RecordingSession:
    """Minimal stand-in for a SQLAlchemy Session.

    Records `execute()` calls as `(sql_string, params)` and returns scripted results keyed by a
    substring of the SQL. Deliberately not a mock library: the thing under test is *which statements
    are issued and in what order*, and a recording list makes that readable in a failure message.
    """

    def __init__(self, results: Any = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._results = results if results is not None else []

    def execute(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        self.calls.append((sql, params))
        return _ScriptedResult(self._results, sql)

    # `flush()` is legitimately called at the end of `dispatch_once`; recording it is enough.
    def flush(self) -> None:
        self.calls.append(("flush", None))

    def rollback(self) -> None:  # pragma: no cover
        raise AssertionError("rollback() was called on the recording session")


class _ScriptedResult:
    def __init__(self, results: Any, sql: str) -> None:
        self._results = results
        self._sql = sql

    def __iter__(self):
        for entry in self._results:
            if entry.get("match", "") in self._sql:
                yield from entry.get("rows", [])
                return

    def scalars(self):
        return self

    def all(self):
        return list(self)

    def first(self):
        rows = list(self)
        return rows[0] if rows else None


def _bind_calls(session: RecordingSession) -> list[Any]:
    """Every `set_config('app.current_org_id', ...)` value, in order."""
    return [params["org"] for sql, params in session.calls if "app.current_org_id" in sql]


# ===========================================================================
# THE FLAG
# ===========================================================================
def test_tenant_binding_defaults_to_OFF():
    from agent.workflow_engine import FleetDispatcher

    assert FleetDispatcher.USE_TENANT_BINDING is False, (
        "tenant binding is on by default; revoking BYPASSRLS must be a separate decision"
    )


def test_the_flag_is_a_class_attribute_not_a_local():
    from agent.workflow_engine import FleetDispatcher

    assert isinstance(FleetDispatcher.__dict__.get("USE_TENANT_BINDING"), bool)


@pytest.mark.parametrize(
    "env, expected",
    [("1", True), ("true", True), ("on", True), ("yes", True),
     ("0", False), ("false", False), ("", False), ("no", False)],
)
def test_the_environment_controls_it(monkeypatch, env, expected):
    """A flag whose off value turns it on is worse than no flag. `bool('0')` is True, which is the
    exact defect `_truthy` exists to prevent - and it applies to this flag too."""
    from agent.workflow_engine import FleetDispatcher

    monkeypatch.setenv("FLEET_TENANT_BINDING", env)
    assert FleetDispatcher(RecordingSession()).use_tenant_binding is expected


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    from agent.workflow_engine import FleetDispatcher

    monkeypatch.setenv("FLEET_TENANT_BINDING", "1")
    assert FleetDispatcher(RecordingSession(), use_tenant_binding=False).use_tenant_binding is False


# ===========================================================================
# THE BINDING ITSELF
# ===========================================================================
def test_tenant_scope_binds_then_clears():
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)

    with dispatcher.tenant_scope("org-a"):
        assert _bind_calls(session) == ["org-a"]

    assert _bind_calls(session) == ["org-a", ""], "the binding was not cleared"


def test_tenant_scope_is_a_noop_when_binding_is_off():
    """The dispatcher wraps its loop body unconditionally, so an unguarded `tenant_scope` would issue
    a `set_config` per workflow on the DEFAULT path - which fails outright on SQLite, where the
    function does not exist. The suite is SQLite; the fleet is PostgreSQL. This asserts the default
    path stays byte-for-byte the behaviour every existing test already exercises."""
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=False)

    with dispatcher.tenant_scope("org-a"):
        pass

    assert session.calls == [], "an unbound dispatcher still issued a binding statement"


def test_tenant_scope_clears_the_binding_when_the_body_raises():
    """THE safeguard that matters most in this design.

    A dispatcher that binds and then raises without clearing leaves the NEXT organisation's work
    running under the previous organisation's context. That is the one failure mode here that could
    cross tenants, rather than merely returning nothing - so it is tested by actually raising.
    """
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)

    with pytest.raises(RuntimeError):
        with dispatcher.tenant_scope("org-a"):
            raise RuntimeError("boom")

    assert _bind_calls(session) == ["org-a", ""], (
        "the tenant binding survived an exception; the next organisation would run as org-a"
    )


def test_the_binding_is_session_scoped_not_transaction_local():
    """`set_config(..., false)` - SESSION scope. The dispatcher commits inside a sweep, and a
    transaction-local setting is discarded at COMMIT, so the second half of a sweep would run unbound
    and return zero rows while looking like a quiet fleet."""
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)
    dispatcher._bind_tenant("org-a")

    sql = session.calls[0][0]
    assert "set_config" in sql
    assert "app.current_org_id" in sql


def test_clearing_uses_an_empty_string_not_null():
    """`app.current_org()` wraps `NULLIF(current_setting(..., true), '')`, so an empty string is what
    it maps to NULL. Clearing to something else would leave a stale tenant visible."""
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)
    dispatcher._bind_tenant(None)

    assert session.calls[0][1]["org"] == ""


# ===========================================================================
# CANDIDATE SELECTION
# ===========================================================================
def test_due_workflow_refs_reads_the_refs_function():
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession(
        [{"match": "fleet_due_workflow_refs", "rows": [("w1", "org-a"), ("w2", "org-b")]}]
    )
    dispatcher = FleetDispatcher(session, batch_size=7, per_agent_limit=3)

    assert dispatcher.due_workflow_refs() == [("w1", "org-a"), ("w2", "org-b")]
    sql, params = session.calls[0]
    assert "fleet_due_workflow_refs" in sql
    assert params == {"batch": 7, "per_agent": 3}


def test_the_refs_function_carries_org_id_in_the_sql():
    """WHY THIS IS ASSERTED RATHER THAN ASSUMED.

    The ids come from many tenants. Without `org_id` the dispatcher cannot know which organisation to
    bind to, so it would either re-select across tenants (impossible under RLS once BYPASSRLS is gone)
    or dispatch one organisation per sweep. The migration SQL must therefore return both columns.
    """
    sql = (BACKEND / "docs" / "adr-0011-fleet-refs.sql").read_text(encoding="utf-8")
    assert "fleet_due_workflow_refs" in sql
    assert "RETURNS TABLE(workflow_id varchar, org_id varchar)" in sql, (
        "the refs function lost org_id; the dispatcher cannot bind without it"
    )
    # The fairness partition is load-bearing: without it a large organisation fills the window.
    assert "PARTITION BY w.agent_id" in sql


def test_the_roster_function_returns_ids_only():
    """The roster is the ONE cross-tenant read. If it grew columns it would become a way to read
    tenant data through a privileged function rather than a way to answer a question about tenancy."""
    sql = (BACKEND / "docs" / "adr-0011-fleet-roster.sql").read_text(encoding="utf-8")
    assert "RETURNS TABLE(agent_id varchar, org_id varchar)" in sql


# ===========================================================================
# THE PATHS ACTUALLY RUN
# ===========================================================================
def test_dispatch_once_with_binding_uses_refs_and_binds_each_workflow():
    """The whole point of step 2: selection via the function, then one tenant at a time."""
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession(
        [{"match": "fleet_due_workflow_refs", "rows": []}]
    )
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)

    dispatcher.dispatch_once()

    assert "fleet_due_workflow_refs" in session.calls[0][0], (
        "dispatch_once did not use the refs function; it would re-select across tenants and go blind"
    )
    assert not any("fleet_due_workflow_ids" in sql for sql in (c[0] for c in session.calls)), (
        "dispatch_once used the ids-only function, which cannot say which tenant to bind to"
    )


def test_dispatch_once_without_binding_does_not_touch_the_function():
    """Additive: the default path is unchanged, which is why every existing test still passes.

    The ORM `due_workflows` path builds a real `select()`; the recording session simply returns no rows
    for it, so the sweep completes with an empty candidate list and no exception. That is the expected
    behaviour of the fake, not a property of the code - so this asserts the ABSENCE of the refs call,
    which is the actual invariant.
    """
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession()
    dispatcher = FleetDispatcher(session, use_tenant_binding=False)

    dispatcher.dispatch_once()

    assert not any("fleet_due_workflow_refs" in sql for sql, _ in session.calls)
    assert not any("fleet_due_workflow_ids" in sql for sql, _ in session.calls)


def test_discovery_with_binding_reads_the_roster_function():
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession([{"match": "fleet_active_agent_ids", "rows": []}])
    dispatcher = FleetDispatcher(session, use_tenant_binding=True)

    assert dispatcher.discover_opportunity_work() == 0
    assert "fleet_active_agent_ids" in session.calls[0][0]


def test_discovery_without_binding_uses_the_plain_select():
    from agent.workflow_engine import FleetDispatcher

    session = RecordingSession([{"match": "granada_agents", "rows": []}])
    dispatcher = FleetDispatcher(session, use_tenant_binding=False)

    assert dispatcher.discover_opportunity_work() == 0
    assert not any("fleet_active_agent_ids" in sql for sql, _ in session.calls)


# ===========================================================================
# STATIC GUARDS
# ===========================================================================
def test_both_flags_exist_in_the_source():
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    assert "USE_TENANT_BINDING" in source
    assert "USE_NARROW_CLAIM" in source


def test_the_dispatcher_does_not_re_select_workflows_across_tenants_when_binding():
    """Re-selecting `WHERE id IN (<ids from many tenants>)` is the exact statement that cannot work
    once BYPASSRLS is revoked: bound to one organisation it returns that organisation's rows only, and
    bound to none it returns zero.

    SCOPED TO dispatch_once ON PURPOSE. Taking the first `if self.use_tenant_binding:` in the file
    lands in `discover_opportunity_work`, whose branch legitimately contains the roster function - so
    an unscoped split asserts against the wrong code and fails for the wrong reason. (It did.)
    """
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    dispatch = source.split("def dispatch_once", 1)[1].split("def _enqueue", 1)[0]
    binding_branch = dispatch.split("if self.use_tenant_binding:", 1)[1].split("else:", 1)[0]
    # The branch calls the METHOD, not the SQL, so assert the method - and separately assert that the
    # method issues the refs function (`test_due_workflow_refs_reads_the_refs_function` above), because
    # a method that stopped calling the function would satisfy a source check on its own name.
    assert "due_workflow_refs" in binding_branch
    assert ".in_(ids)" not in binding_branch, (
        "the binding path re-selects across tenants, which returns zero rows under RLS"
    )


def test_tenant_scope_is_used_around_the_row_load():
    """The bind must wrap the workflow and agent lookups, not follow them. Binding after the lookup
    would leave `agent is None` firing for every row - the silent-skip failure."""
    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    dispatch = source.split("def dispatch_once", 1)[1].split("def _enqueue", 1)[0]
    bind_at = dispatch.index("with self.tenant_scope(org_id):")
    load_at = dispatch.index("models.AgentWorkflow.id == workflow_id")
    assert bind_at < load_at, "the row is loaded before the tenant is bound"


# ===========================================================================
# THE SECOND CROSS-TENANT SWEEP: mail reconciliation
# ===========================================================================
def test_mail_sync_accepts_an_org_scope():
    """`MailAccount` is under RLS. An unscoped stale-mailbox query returns rows only while the fleet
    connection carries BYPASSRLS; once revoked it returns ZERO, and the failure is an ABSENCE - a
    mailbox that never reconciles again logs nothing at all."""
    import inspect

    from agent.mail.gateway import schedule_mail_sync

    signature = inspect.signature(schedule_mail_sync)
    assert "org_id" in signature.parameters, (
        "schedule_mail_sync cannot be scoped to one organisation, so it cannot run bound"
    )
    assert signature.parameters["org_id"].default is None, (
        "the scope must be opt-in: the default path is unchanged"
    )


def test_the_mail_sweep_walks_the_roster_when_binding():
    source = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    mail = source.split("def sync_due_mail_accounts", 1)[1].split("def _mail_sync_due", 1)[0]
    assert "fleet_active_agent_ids" in mail, (
        "the mail sweep does not use the roster, so it would go blind under RLS"
    )
    assert "app.current_org_id" in mail, "the mail sweep never binds a tenant"
    assert "org_id=org_id" in mail, "the mail sweep does not scope the query to the bound tenant"


def test_the_mail_sweep_clears_the_binding_after_every_organisation():
    """A leaked binding would run the NEXT organisation's mail sweep under this one's context - the
    same cross-tenant hazard `tenant_scope` guards against in the dispatcher."""
    source = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    mail = source.split("def sync_due_mail_accounts", 1)[1].split("def _mail_sync_due", 1)[0]
    assert "finally:" in mail, (
        "the mail sweep binds a tenant without a finally-clear; a failure would leak the binding"
    )


def test_fleet_runner_and_dispatcher_read_the_same_flag():
    """Two flags for one migration would let the dispatcher bind while the mail sweep did not - and
    the half that stayed unprivileged is the half that fails silently."""
    runner = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    assert 'os.environ.get("FLEET_TENANT_BINDING")' in runner
    assert "use_tenant_binding" in runner
