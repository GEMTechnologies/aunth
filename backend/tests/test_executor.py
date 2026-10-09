"""The executor: the process that was missing.

THE GAP. `AgentWorker.execute(job_id)` existed and was tested, and nothing called it. The deployment
ran one fleet process - `agent.fleet_runner` - which is dispatch-only by its own docstring. Measured
on the VPS on 2026-10-09, after the dispatcher could finally see its own table:

    workflows=2 matches=0 jobs=2 attempts=0
    JOB opportunity_match | QUEUED

Two jobs, queued, never attempted.

These tests cover the SELECTION and the LOOP - what this module owns. What happens inside a job is
`AgentWorker`'s contract and is covered by the fleet and workspace suites.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.executor import HEARTBEAT_PATH, JobExecutor, worker_identity  # noqa: E402

ROOT = BACKEND.parent.parent
COMPOSE = ROOT / "docker-compose.yml"


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "executor.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _factory(db):
    """A session factory over the same database, as the dispatcher's tests do."""
    engine = db.get_bind()
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _org(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Executor Test", slug=f"org-{uuid.uuid4().hex[:8]}",
        owner_user_id=user.id,
    )
    db.add(row)
    db.commit()
    return row


def _job(db, org, *, state=None, available_at=None, **overrides):
    payload = {
        "id": str(uuid.uuid4()),
        "org_id": org.id,
        "stream": "default",
        "job_type": "opportunity_match",
        "idempotency_key": uuid.uuid4().hex,
        "payload": {},
        "state": state or models.Job.QUEUED,
        "attempt": 0,
        "max_attempts": 3,
        "available_at": available_at or datetime.now(timezone.utc) - timedelta(minutes=1),
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    row = models.Job(**payload)
    db.add(row)
    db.commit()
    return row


# ===========================================================================
# SELECTION
# ===========================================================================
def test_a_queued_job_is_a_candidate(db):
    org = _org(db)
    job = _job(db, org)
    assert [str(job.id)] == JobExecutor(_factory(db)).claimable_jobs(db)


def test_a_job_in_the_future_is_NOT_a_candidate(db):
    """`available_at` is the backoff. A retry that is not due yet must not be attempted - attempting
    it burns an attempt on a job the ledger deliberately delayed."""
    org = _org(db)
    _job(db, org, available_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert JobExecutor(_factory(db)).claimable_jobs(db) == []


@pytest.mark.parametrize("state", ["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_finished_or_running_job_is_NOT_a_candidate(db, state):
    org = _org(db)
    _job(db, org, state=state)
    assert JobExecutor(_factory(db)).claimable_jobs(db) == []


def test_the_candidate_query_is_bounded(db):
    """A backlog of 10,000 must drain steadily rather than load 10,000 rows into one transaction."""
    org = _org(db)
    for _ in range(40):
        _job(db, org)
    executor = JobExecutor(_factory(db), batch_size=10)
    assert len(executor.claimable_jobs(db)) == 10


def test_the_oldest_due_job_comes_first(db):
    """Ordering matters for a backlog: without it a job can be starved indefinitely."""
    org = _org(db)
    now = datetime.now(timezone.utc)
    newer = _job(db, org, available_at=now - timedelta(minutes=1))
    older = _job(db, org, available_at=now - timedelta(minutes=30))
    assert JobExecutor(_factory(db)).claimable_jobs(db) == [str(older.id), str(newer.id)]


# ===========================================================================
# THE PASS
# ===========================================================================
def test_a_pass_attempts_the_queued_job(db):
    """THE test for the missing process: a queued job is finally attempted."""
    org = _org(db)
    _job(db, org)
    result = JobExecutor(_factory(db)).run_once()
    assert result.attempted == 1, "the executor did not pick up a queued job"


def test_a_pass_with_nothing_due_attempts_nothing(db):
    """The inverse, so the test above is not satisfied by attempting everything."""
    org = _org(db)
    _job(db, org, state=models.Job.SUCCEEDED)
    result = JobExecutor(_factory(db)).run_once()
    assert result.attempted == 0


def test_it_executes_with_a_self_identifying_worker_id():
    """Leases are attributable, so a stuck job can be traced to a container."""
    identity = worker_identity()
    assert identity.startswith("executor:")
    assert identity.count(":") >= 2, "a lease with no host and pid cannot be traced to anything"


def test_an_empty_worker_id_is_refused():
    """`AgentWorker` refuses it, and the executor must not construct one that does."""
    from agent.workflow_engine import FleetError

    with pytest.raises(FleetError):
        from agent.workflow_engine import AgentWorker

        AgentWorker(None, worker_id="")


def test_one_bad_job_does_not_stop_the_pass(db, monkeypatch):
    """Containment: the remaining jobs are other organisations' work."""
    org = _org(db)
    _job(db, org)
    _job(db, org)

    from agent import workflow_engine

    original = workflow_engine.AgentWorker.execute
    calls = {"n": 0}

    def explode(self, job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the provider exploded")
        return original(self, job_id)

    monkeypatch.setattr(workflow_engine.AgentWorker, "execute", explode)
    result = JobExecutor(_factory(db)).run_once()
    assert result.attempted == 2, "the pass stopped after the first job raised"
    assert result.errors, "the crash was not recorded"


def test_a_pass_is_bounded_by_batch_size(db):
    org = _org(db)
    for _ in range(30):
        _job(db, org)
    result = JobExecutor(_factory(db), batch_size=5).run_once()
    assert result.attempted == 5


# ===========================================================================
# THE LOOP
# ===========================================================================
def test_a_bounded_run_TERMINATES(db):
    """THE regression guard for the bug that made a previous version of this work hang.

    An earlier change to the sweep left a name unbound, so every pass raised, the pass counter never
    advanced, and `run_forever(max_passes=2)` span forever at one second a tick. The existing test
    that caught it did so by HANGING. This one asserts the counter, so the failure is a fast red
    rather than a CI job that never returns.
    """
    org = _org(db)
    _job(db, org)
    health = JobExecutor(_factory(db), interval_seconds=0.01).run_forever(max_passes=2)
    assert health.sweeps == 2, "the bounded loop did not reach its bound"
    assert health.running is False, "a bounded run should finish"


def test_the_loop_reports_errors_rather_than_growing_in_silence(db):
    """A pass that cannot even open a session must be counted, not swallowed."""
    def broken_factory():
        raise RuntimeError("the database is gone")

    health = JobExecutor(broken_factory, interval_seconds=0.01).run_forever(max_passes=1)
    assert health.errors >= 1
    assert health.last_error and "the database is gone" in health.last_error


# ===========================================================================
# THE DEPLOYMENT
# ===========================================================================
def test_the_executor_is_a_DEPLOYED_service():
    """The whole gap: it existed and nothing ran it."""
    import yaml

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert "executor" in services, "no executor service, so jobs stay QUEUED forever"
    assert services["executor"]["command"] == ["python", "-m", "agent.executor"]


def test_the_executor_has_the_fleet_credential():
    """`jobs` is FORCE RLS, so under the application role the claim query returns zero rows and the
    executor idles while reporting itself healthy (ADR-0011)."""
    import yaml

    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["executor"]["environment"]
    assert "FLEET_DATABASE_URL" in env
    assert "granada_fleet" in env["FLEET_DATABASE_URL"]


def test_the_executor_heartbeat_is_NOT_the_dispatchers():
    """A wedged executor must be visible as an executor problem. One shared file would let a stuck
    worker look like a healthy dispatcher, or the reverse."""
    from agent.heartbeat import DEFAULT_HEARTBEAT_PATH

    assert str(DEFAULT_HEARTBEAT_PATH) != str(HEARTBEAT_PATH)

    import yaml

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    executor_test = services["executor"]["healthcheck"]["test"][1]
    dispatcher_test = services["worker"]["healthcheck"]["test"][1]
    assert "granada-executor-heartbeat" in executor_test
    assert "granada-executor-heartbeat" not in dispatcher_test


def test_the_executor_uses_the_fleet_session_factory():
    """Read from the source: the entry point must not use the request-path factory."""
    source = (BACKEND / "agent" / "executor.py").read_text(encoding="utf-8")
    assert "FleetSessionLocal" in source
