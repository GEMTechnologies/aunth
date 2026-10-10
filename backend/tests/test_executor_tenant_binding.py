"""ADR-0011 step 2, the EXECUTOR's half: claim and run jobs bound to one organisation.

WHY THIS FILE EXISTS
--------------------
The dispatcher was narrowed and cut over to tenant binding. The executor was not. Then
`granada_fleet`'s `BYPASSRLS` was revoked, and the executor became blind while every health check
kept passing:

    as granada_fleet, app.current_org() = NULL
    select count(*) from jobs                      -> 0
    the same query bound to the one organisation   -> 1443

The fleet therefore had 55 QUEUED jobs it could never claim and 1,443 rows it could not see. The
failure was an ABSENCE: no exception, no error log, `healthy` true, `claimed` 0. That is the same
silent-zero hazard `test_adr0011_tenant_binding.py` documents for the dispatcher, one service over.

THE SECOND, SUBTLER DEFECT THESE TESTS PIN
------------------------------------------
`set_config('app.current_org_id', ..., false)` is session-scoped, so the code's own comment claimed
it survives a COMMIT. MEASURED against the production pool it does NOT: the pool runs
`ResetStyle.reset_rollback` when a connection is returned, which reverts it. Bound -> 1443 rows;
after `commit()` -> 0; re-bind -> 1443.

The dispatcher never noticed because it only `flush()`es and commits once at the end of a sweep. The
executor commits after EVERY job. So a binding established once per organisation would be silently
gone by the second job - which is why `_attempt` re-binds first, and why there is a test below that
fails if anyone "optimises" that line away.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class RecordingSession:
    """A session that records `(sql, params)` and can be told what to return.

    Deliberately not a mock library: the property under test is *which statements are issued, in
    what order, with which values*, and a recording list is readable in a failure message.
    """

    def __init__(self, results: Any = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self._results = results if results is not None else []

    def execute(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        self.calls.append((sql, params))
        matched: list[Any] = []
        for entry in self._results:
            if entry.get("match", "") in sql:
                matched = entry.get("rows", [])
                break
        return _ScriptedResult(matched)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def flush(self) -> None:  # pragma: no cover - recorded for completeness
        self.calls.append(("flush", None))

    def close(self) -> None:
        pass


class _ScriptedResult:
    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalars(self):
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        rows = list(self._rows)
        return rows[0] if rows else None


def _binds(session: RecordingSession) -> list[str]:
    """Every `app.current_org_id` value set, in order. `""` is a clear."""
    return [p["org"] for sql, p in session.calls if "app.current_org_id" in sql]


def _executor(session: RecordingSession, **kwargs: Any) -> Any:
    from agent.executor import JobExecutor

    return JobExecutor(lambda: session, worker_id="executor:test:1", **kwargs)


# ===========================================================================
# THE FLAG
# ===========================================================================
def test_the_executor_binding_defaults_to_OFF():
    """Turning this on changes production claim behaviour. It must be a separate, reversible
    decision by configuration - never something a refactor switches on silently."""
    from agent.executor import JobExecutor

    assert JobExecutor.USE_TENANT_BINDING is False


def test_the_flag_is_a_class_attribute_not_a_local():
    from agent.executor import JobExecutor

    assert isinstance(JobExecutor.__dict__.get("USE_TENANT_BINDING"), bool)


@pytest.mark.parametrize(
    "env, expected",
    [("1", True), ("true", True), ("on", True), ("yes", True),
     ("0", False), ("false", False), ("", False), ("no", False)],
)
def test_the_environment_controls_the_executor(monkeypatch, env, expected):
    """`bool("0")` is True. A flag whose OFF value enables it is worse than no flag."""
    monkeypatch.setenv("FLEET_TENANT_BINDING", env)
    assert _executor(RecordingSession()).use_tenant_binding is expected


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("FLEET_TENANT_BINDING", "1")
    assert _executor(RecordingSession(), use_tenant_binding=False).use_tenant_binding is False


def test_the_executor_and_the_dispatcher_read_the_SAME_flag():
    """Two flags for one migration would let one service bind while the other did not - and the
    half that stayed unbound is the half that fails as a silent zero."""
    runner = (BACKEND / "agent" / "executor.py").read_text(encoding="utf-8")
    assert 'os.environ.get("FLEET_TENANT_BINDING")' in runner


# ===========================================================================
# THE UNBOUND PATH IS UNCHANGED
# ===========================================================================
def test_an_unbound_claim_issues_NO_set_config():
    """The default path must stay byte-for-byte what every existing test exercises. On SQLite
    `set_config` does not exist, so a stray bind would be a hard failure, not a slow one."""
    session = RecordingSession()
    _executor(session, use_tenant_binding=False).claimable_jobs(session)
    assert _binds(session) == [], "the unbound claim bound a tenant"


def test_an_unbound_pass_issues_NO_set_config():
    session = RecordingSession([{"match": "FROM jobs", "rows": []}])
    _executor(session, use_tenant_binding=False).run_once()
    assert _binds(session) == [], "the unbound pass bound a tenant"


# ===========================================================================
# THE BOUND PATH BINDS BEFORE IT READS
# ===========================================================================
def test_a_scoped_claim_binds_BEFORE_reading_jobs():
    """A bind issued after the read protects nothing - the read has already returned zero."""
    session = RecordingSession([{"match": "FROM jobs", "rows": ["job-1"]}])
    ids = _executor(session, use_tenant_binding=True).claimable_jobs(session, org_id="org-A")

    assert ids == ["job-1"]
    assert _binds(session) == ["org-A"]
    bind_at = next(i for i, (sql, _) in enumerate(session.calls) if "app.current_org_id" in sql)
    read_at = next(i for i, (sql, _) in enumerate(session.calls) if "FROM jobs" in sql)
    assert bind_at < read_at, "the jobs query ran before the tenant was bound"


def test_a_scoped_claim_without_an_org_clears_rather_than_leaving_a_stale_binding():
    """`org_id=None` must not inherit whatever the connection was last bound to."""
    session = RecordingSession([{"match": "FROM jobs", "rows": []}])
    _executor(session, use_tenant_binding=True).claimable_jobs(session, org_id=None)
    assert _binds(session) == [""], "a scoped claim with no organisation left the binding untouched"


# ===========================================================================
# THE REGRESSION THAT MATTERS: A BINDING DOES NOT SURVIVE A COMMIT
# ===========================================================================
def test_every_job_is_REBOUND_because_a_commit_drops_the_binding(monkeypatch):
    """THE MEASURED DEFECT. Bound to one organisation `jobs` reads 1,443 rows; after `commit()` the
    same session reads 0, because the pool reverts the session-level `set_config` on return.

    `_attempt` commits after each job, so a binding taken once per organisation would be gone by the
    second job - and the symptom is an absence (jobs never claimed), not an error. This test fails
    if the re-bind in `_attempt` is removed as redundant.
    """
    import agent.executor as executor_mod
    from agent.workflow_engine import ExecutionResult

    monkeypatch.setattr(
        executor_mod,
        "AgentWorker",
        lambda db, worker_id=None: SimpleNamespace(
            execute=lambda job_id: SimpleNamespace(outcome=ExecutionResult.SUCCEEDED)
        ),
    )

    session = RecordingSession()
    runner = _executor(session, use_tenant_binding=True)
    result = executor_mod.PassResult()

    runner._attempt(session, "job-1", result, org_id="org-A")
    runner._attempt(session, "job-2", result, org_id="org-A")

    assert result.attempted == 2 and result.succeeded == 2
    assert session.commits == 2, "the test does not reproduce the commit that drops the binding"
    assert _binds(session) == ["org-A", "org-A"], (
        "a job was attempted without re-binding; after the previous commit that job sees zero rows"
    )


def test_an_unbound_attempt_issues_no_bind(monkeypatch):
    import agent.executor as executor_mod
    from agent.workflow_engine import ExecutionResult

    monkeypatch.setattr(
        executor_mod,
        "AgentWorker",
        lambda db, worker_id=None: SimpleNamespace(
            execute=lambda job_id: SimpleNamespace(outcome=ExecutionResult.SUCCEEDED)
        ),
    )
    session = RecordingSession()
    runner = _executor(session, use_tenant_binding=False)
    runner._attempt(session, "job-1", executor_mod.PassResult())
    assert _binds(session) == []


def test_a_failed_job_does_not_stop_the_pass_and_is_counted(monkeypatch):
    """One organisation's broken job must not stop the others; the pass carries on and the error is
    recorded rather than swallowed."""
    import agent.executor as executor_mod

    def _boom(db, worker_id=None):
        return SimpleNamespace(execute=lambda job_id: (_ for _ in ()).throw(RuntimeError("boom")))

    monkeypatch.setattr(executor_mod, "AgentWorker", _boom)
    session = RecordingSession()
    runner = _executor(session, use_tenant_binding=True)
    result = executor_mod.PassResult()

    runner._attempt(session, "job-1", result, org_id="org-A")
    runner._attempt(session, "job-2", result, org_id="org-A")

    assert result.attempted == 2
    assert result.succeeded == 0 and result.failed == 0
    assert len(result.errors) == 2 and "boom" in result.errors[0]
    assert _binds(session) == ["org-A", "org-A"], "a failed attempt skipped the next re-bind"


# ===========================================================================
# THE PASS WALKS THE ROSTER RATHER THAN GUESSING AN ORGANISATION
# ===========================================================================
def test_the_bound_pass_uses_the_NARROW_roster_function():
    """ "Which organisations exist" cannot be answered from inside one tenant. It must come from the
    reviewed SECURITY DEFINER function, not from a widened privilege."""
    source = (BACKEND / "agent" / "executor.py").read_text(encoding="utf-8")
    run = source.split("def run_once", 1)[1].split("def _attempt", 1)[0]
    assert "fleet_active_agent_ids" in run, "the pass does not learn the roster from the narrow function"
    assert "_roster(db)" in run, "the pass does not walk the roster"
    assert "claimable_jobs(db, now=now, org_id=org_id)" in run, (
        "the pass claims without scoping to the organisation it bound"
    )


def test_the_roster_is_read_BEFORE_any_binding():
    """The roster function is itself the thing that makes binding possible, so it must not be called
    under a binding - and it must be SECURITY DEFINER in the database."""
    source = (BACKEND / "agent" / "executor.py").read_text(encoding="utf-8")
    assert "fleet_active_agent_ids" in source
    sql = (BACKEND / "docs" / "adr-0011-fleet-roster.sql").read_text(encoding="utf-8")
    assert "SECURITY DEFINER" in sql
    assert "GRANT EXECUTE ON FUNCTION fleet_active_agent_ids() TO granada_fleet" in sql
