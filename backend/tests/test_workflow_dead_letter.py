"""A job that dies must not leave its workflow stranded.

THE DEFECT THIS PINS. `ledger.fail` moves the JOB; nothing moved the WORKFLOW. A job that exhausted
its attempts left the workflow in RUNNING with no job behind it, and nothing re-dispatches a RUNNING
workflow - so the work was stranded: never retried, never closed, invisible except as a number that
never went down.

Measured on production: 49 workflows sat in RUNNING after their jobs dead-lettered. The counter that
would have shown it was `workflows by state`, which nobody was watching - and the failure itself
appeared only in an executor log line.

WHY FAILED AND NOT WAITING. WAITING means "due again later". A permanent rejection that has exhausted
its attempts is not going to become true, and parking it as WAITING would promise a retry that can
never succeed.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.workflow_engine import AgentWorker, ExecutionResult  # noqa: E402
from events.ledger import JobLedger  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "deadletter.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="Granada Test Organisation",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()

    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()
    agent = service.get()
    return {"org": org, "user": user, "agent": agent}


def _workflow(db, world):
    """A scheduled workflow, created through the supported entry point rather than by hand."""
    from agent.workflow_engine import WORKFLOW_MATCH

    service = GranadaAgentService(db, world["org"].id)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=str(uuid.uuid4()),
        run_at=datetime.now(timezone.utc),
    )
    db.commit()
    return (
        db.query(models.AgentWorkflow)
        .filter(models.AgentWorkflow.agent_id == world["agent"].id)
        .order_by(models.AgentWorkflow.created_at.desc())
        .first()
    )


def _job_for(db, world, workflow):
    """A job that will fail permanently: no handler owns this job type."""
    job, _ = JobLedger(db).enqueue(
        org_id=world["org"].id,
        job_type="not_a_handled_job_type",
        payload={},
        idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
        max_attempts=1,
    )
    db.flush()
    job.workflow_id = workflow.id
    db.commit()
    return job


def test_a_dead_lettered_job_closes_its_workflow(db, world):
    """The assertion that was missing: the workflow must not stay RUNNING."""
    workflow = _workflow(db, world)
    workflow.state = models.AgentWorkflow.RUNNING
    db.commit()

    job = _job_for(db, world, workflow)
    result = AgentWorker(db, worker_id="w1").execute(job.id)
    db.commit()

    assert result.outcome == ExecutionResult.FAILED
    assert db.get(models.Job, job.id).state == models.Job.DEAD_LETTER

    fresh = db.get(models.AgentWorkflow, workflow.id)
    assert fresh.state == models.AgentWorkflow.FAILED, (
        "a RUNNING workflow with no job behind it is stranded: nothing re-dispatches it"
    )
    assert fresh.waiting_on and "dead-lettered" in fresh.waiting_on, (
        "the reason must be recorded, or an operator sees FAILED with no story"
    )
    assert fresh.next_run_at is None, "FAILED must not carry a future run time that implies a retry"


def test_no_workflow_is_left_running_in_the_database(db, world):
    """The blunt check. On production 49 workflows were RUNNING with dead jobs behind them, and the
    only symptom was a number that never came down."""
    for _ in range(3):
        workflow = _workflow(db, world)
        workflow.state = models.AgentWorkflow.RUNNING
        db.commit()
        _job_for(db, world, workflow)

    running = (
        db.query(models.AgentWorkflow)
        .filter(models.AgentWorkflow.state == models.AgentWorkflow.RUNNING)
        .all()
    )
    assert len(running) == 3, "precondition: they start RUNNING"

    for workflow in running:
        job = (
            db.query(models.Job)
            .filter(models.Job.workflow_id == workflow.id)
            .first()
        )
        AgentWorker(db, worker_id="w1").execute(job.id)
        db.commit()

    stranded = (
        db.query(models.AgentWorkflow)
        .filter(models.AgentWorkflow.state == models.AgentWorkflow.RUNNING)
        .all()
    )
    assert stranded == [], f"{len(stranded)} workflow(s) left RUNNING with no job behind them"
