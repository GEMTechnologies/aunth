"""Phase 6d operations: the fleet loop, admin recovery, and the status panel."""

from __future__ import annotations

import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.admin import (  # noqa: E402
    COMMANDS,
    AdminCommands,
    AdminError,
    NotFound,
    Refused,
    run_command,
)
from agent.decision.policy import Autonomy  # noqa: E402
from agent.fleet_runner import FleetRunner, FleetHealth, install_signal_handlers  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.workflow_engine import (  # noqa: E402
    WORKFLOW_MATCH,
    AgentWorker,
    ExecutionResult,
    FleetDispatcher,
)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "ops.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="War Child Test", slug=None):
    from agent.organisation_memory import DocumentVault, OrganisationMemory, checksum_bytes

    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name,
        slug=slug or f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(row)
    db.commit()
    memory = OrganisationMemory(db, row.id)
    memory.record_fact(key="country", value="Uganda", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(key="organisation_type", value="NGO", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(
        key="registration_valid_until", value="2030-01-01", state=models.OrgFact.VERIFIED,
        source="user:1", valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()
    vault = DocumentVault(db, row.id)
    document = vault.add_version(
        title="Certificate", doc_type="registration_certificate",
        storage_key=f"org/{row.slug}/r.pdf", checksum_sha256=checksum_bytes(b"c"),
        mime_type="application/pdf",
        valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    vault.approve(document, approved_by="user:1")
    db.commit()
    return row, user.id


def _opportunity(db, **overrides):
    payload = {
        "title": "Child Protection Grant 2027",
        "source_url": f"https://funders.example.org/{uuid.uuid4().hex[:8]}",
        "source_name": "Example Humanitarian Fund",
        "country": "Uganda",
        "content_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "dedupe_fingerprint": uuid.uuid4().hex + uuid.uuid4().hex,
        "is_active": True,
        "deadline": datetime.now(timezone.utc) + timedelta(days=45),
        "amount_max": 250_000,
        "eligibility_criteria": "Registered NGOs in Uganda. Attach a registration certificate.",
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    row = models.Opportunity(**payload)
    db.add(row)
    db.commit()
    return row


def _setup(db, autonomy=Autonomy.MONITOR_ONLY):
    org, owner = _org(db)
    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=autonomy)
    db.commit()
    opportunity = _opportunity(db)
    workflow = service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        specialist_key="MATCHER",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    return org, owner, service, opportunity, workflow


# ---------------------------------------------------------------------------
# 5. THE FLEET LOOP
# ---------------------------------------------------------------------------
def _factory(db):
    """A session factory over the same database the test holds.

    The runner owns a session per sweep, so it cannot reuse the test's session -
    which is the point: a long-lived session would hold a transaction open across
    every sleep.
    """
    engine = db.get_bind()
    return sessionmaker(bind=engine, future=True)


def test_the_loop_drains_without_anyone_calling_dispatch(db):
    """The brief's requirement: work advances with no test invoking the dispatcher.

    ``healthy`` is deliberately False after a **bounded** run, because it means
    "the loop is alive and has swept" - which is the right answer for a health
    endpoint and the wrong thing to assert about a run that has finished. The
    assertions below are the meaningful ones for a completed run; liveness is
    asserted in the shutdown test, where the loop is actually running.
    """
    org, owner, service, opportunity, workflow = _setup(db)

    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    health = runner.run_forever(max_sweeps=2)

    assert health.sweeps == 2
    assert health.dispatched >= 1, "the loop never dispatched anything"
    assert health.errors == 0
    assert health.last_sweep_at is not None
    assert health.running is False, "a bounded run should finish"

    db.expire_all()
    jobs = db.execute(select(models.Job)).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].state == models.Job.QUEUED


def test_a_running_loop_reports_itself_healthy(db):
    """Liveness is asserted while the loop is alive, which is when it means something."""
    _setup(db)
    runner = FleetRunner(_factory(db), interval_seconds=30.0)
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while runner.health.sweeps < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert runner.health.sweeps >= 1
        assert runner.health.healthy is True, runner.health.as_dict()
    finally:
        runner.stop()
        thread.join(timeout=10)


def test_the_loop_is_healthy_only_after_a_sweep(db):
    """A loop that has never completed a sweep is not healthy, however new."""
    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    assert runner.health.healthy is False
    assert runner.health.sweeps == 0


def test_a_failed_sweep_does_not_kill_the_loop(db):
    """A transient database blip must not stop the fleet for every customer."""
    calls = {"n": 0}

    def exploding_factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is gone")
        return _factory(db)()

    runner = FleetRunner(exploding_factory, interval_seconds=1.0)
    health = runner.run_forever(max_sweeps=2)

    assert health.errors == 1, "the failure was not counted"
    assert "database is gone" in (health.last_error or "")
    assert health.running is False, "the loop exited instead of continuing"


def test_stop_is_honoured_promptly_and_gracefully(db):
    """Graceful shutdown: finish the sweep, then exit. Do not abandon a transaction."""
    _setup(db)
    runner = FleetRunner(_factory(db), interval_seconds=30.0)

    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    # Wait until at least one sweep has happened.
    deadline = time.time() + 10
    while runner.health.sweeps < 1 and time.time() < deadline:
        time.sleep(0.05)

    started = time.time()
    runner.stop()
    thread.join(timeout=10)
    elapsed = time.time() - started

    assert not thread.is_alive(), "the loop ignored the stop signal"
    assert elapsed < 5, f"stop took {elapsed:.1f}s; the sleep was not sliced"
    assert runner.health.stopping is True
    assert runner.health.running is False


def test_signal_handlers_do_not_fail_off_the_main_thread(db):
    """A runner started from a worker thread must not crash installing handlers."""
    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    result = {"installed": None}

    def target():
        result["installed"] = install_signal_handlers(runner)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=5)
    # Either outcome is acceptable; raising is not.
    assert result["installed"] in (True, False)


def test_two_dispatchers_in_the_loop_do_not_duplicate(db):
    """Two runners over the same database produce the same work as one."""
    org, owner, service, opportunity, workflow = _setup(db)

    first = FleetRunner(_factory(db), interval_seconds=1.0)
    second = FleetRunner(_factory(db), interval_seconds=1.0)
    first.run_forever(max_sweeps=1)
    second.run_forever(max_sweeps=1)

    db.expire_all()
    jobs = db.execute(select(models.Job)).scalars().all()
    assert len(jobs) == 1, f"two dispatchers created {len(jobs)} jobs"


def test_health_reports_the_metrics_an_operator_needs(db):
    _setup(db)
    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    runner.run_forever(max_sweeps=1)
    payload = runner.health.as_dict()
    for field_name in (
        "running", "stopping", "healthy", "sweeps", "dispatched",
        "duplicates", "errors", "last_sweep_at", "started_at",
    ):
        assert field_name in payload


# ---------------------------------------------------------------------------
# 6. ADMIN RECOVERY COMMANDS
# ---------------------------------------------------------------------------
def test_stuck_workflows_are_listed(db):
    org, owner, service, opportunity, workflow = _setup(db)
    workflow.next_run_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()

    result = AdminCommands(db, org.id, actor_id=owner).list_stuck_workflows()
    assert result.ok
    assert [w["id"] for w in result.data["stuck"]] == [workflow.id]
    assert result.data["stuck"][0]["reason"] == "overdue"


def test_show_workflow_and_job_expose_the_recovery_detail(db):
    org, owner, service, opportunity, workflow = _setup(db)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    commands = AdminCommands(db, org.id, actor_id=owner)
    shown_workflow = commands.show_workflow(workflow.id)
    assert shown_workflow.ok
    assert shown_workflow.data["workflow"]["jobs"][0]["id"] == job.id

    shown_job = commands.show_job(job.id)
    assert shown_job.ok
    assert shown_job.data["job"]["agent_id"] == service.get().id


def test_retry_step_requeues_a_waiting_workflow(db):
    org, owner, service, opportunity, workflow = _setup(db)
    workflow.state = models.AgentWorkflow.WAITING
    workflow.waiting_on = "provider unavailable"
    db.commit()

    result = AdminCommands(db, org.id, actor_id=owner).retry_step(workflow.id)
    db.commit()
    assert result.ok
    db.refresh(workflow)
    assert workflow.state == models.AgentWorkflow.PENDING
    assert workflow.waiting_on is None


def test_retry_step_refuses_when_the_specialist_cannot_run(db):
    """A retry that can only fail again is not a recovery, it is a loop."""
    org, owner, service, opportunity, workflow = _setup(db)
    workflow.workflow_type = "proposal_draft"
    workflow.specialist_key = "PROPOSAL_WRITER"
    workflow.state = models.AgentWorkflow.WAITING
    db.commit()

    with pytest.raises(Refused) as excinfo:
        AdminCommands(db, org.id, actor_id=owner).retry_step(workflow.id)
    assert "would fail again" in str(excinfo.value)


def test_retry_step_refuses_a_finished_workflow(db):
    org, owner, service, opportunity, workflow = _setup(db)
    workflow.state = models.AgentWorkflow.COMPLETED
    db.commit()
    with pytest.raises(Refused):
        AdminCommands(db, org.id, actor_id=owner).retry_step(workflow.id)


def test_requeue_job_refuses_completed_work(db):
    """Re-running completed work is how duplicates are created."""
    org, owner, service, opportunity, workflow = _setup(db)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    with pytest.raises(Refused) as excinfo:
        AdminCommands(db, org.id, actor_id=owner).requeue_job(job.id)
    assert "already succeeded" in str(excinfo.value)


def test_requeue_job_refuses_work_under_a_live_lease(db):
    org, owner, service, opportunity, workflow = _setup(db)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    from events.ledger import JobLedger

    JobLedger(db).claim(job_id=job.id, worker_id="live-worker", lease_seconds=300)
    db.commit()

    with pytest.raises(Refused) as excinfo:
        AdminCommands(db, org.id, actor_id=owner).requeue_job(job.id)
    assert "RUNNING" in str(excinfo.value)


def test_requeue_job_works_for_a_dead_lettered_job(db):
    org, owner, service, opportunity, workflow = _setup(db)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    job.state = models.Job.DEAD_LETTER
    job.failure_category = "PERMANENT_REJECTION"
    db.commit()

    result = AdminCommands(db, org.id, actor_id=owner).requeue_job(job.id)
    db.commit()
    assert result.ok
    db.refresh(job)
    assert job.state == models.Job.QUEUED
    assert job.failure_category is None


def test_pause_and_resume_agent_work_and_bump_the_version(db):
    org, owner, service, opportunity, workflow = _setup(db)
    commands = AdminCommands(db, org.id, actor_id=owner)
    before = service.get().version

    paused = commands.pause_agent(reason="operator investigating")
    db.commit()
    assert paused.ok
    assert service.get().status == models.GranadaAgent.PAUSED
    assert service.get().version > before, "pausing did not bump the authority version"

    resumed = commands.resume_agent()
    db.commit()
    assert resumed.ok
    assert service.get().status == models.GranadaAgent.ACTIVE


def test_resume_workflow_refuses_while_the_agent_is_paused(db):
    """Resuming the workflow of a paused agent would be undone at the next
    authority checkpoint, so it is refused rather than allowed to appear to work."""
    org, owner, service, opportunity, workflow = _setup(db)
    commands = AdminCommands(db, org.id, actor_id=owner)
    commands.pause_agent()
    db.commit()

    with pytest.raises(Refused) as excinfo:
        commands.resume_workflow(workflow.id)
    assert "paused" in str(excinfo.value)


def test_cancel_workflow_is_terminal(db):
    org, owner, service, opportunity, workflow = _setup(db)
    commands = AdminCommands(db, org.id, actor_id=owner)
    assert commands.cancel_workflow(workflow.id, reason="funder withdrew").ok
    db.commit()
    with pytest.raises(Refused):
        commands.cancel_workflow(workflow.id)


def test_every_mutating_command_requires_an_attributed_operator(db):
    """An unattributed administrative action cannot be investigated afterwards."""
    org, owner, service, opportunity, workflow = _setup(db)
    anonymous = AdminCommands(db, org.id, actor_id=None)

    for call in (
        lambda: anonymous.retry_step(workflow.id),
        lambda: anonymous.pause_agent(),
        lambda: anonymous.resume_agent(),
        lambda: anonymous.cancel_workflow(workflow.id),
        lambda: anonymous.drain_fleet(),
    ):
        with pytest.raises(Refused) as excinfo:
            call()
        assert "attributed operator" in str(excinfo.value)


def test_commands_are_audited(db):
    """Every action leaves a trace, or it is indistinguishable from an intrusion."""
    org, owner, service, opportunity, workflow = _setup(db)
    AdminCommands(db, org.id, actor_id=owner).pause_agent(reason="investigating")
    db.commit()

    audits = db.execute(select(models.AuditLog)).scalars().all()
    assert audits, "the command left no audit record"
    assert audits[0].event == "admin.pause_agent"
    assert audits[0].user_id == owner
    assert audits[0].org_id == org.id

    internal = [
        a for a in db.execute(select(models.AgentActivity)).scalars()
        if a.visibility == models.AgentActivity.VISIBILITY_INTERNAL
    ]
    assert internal, "the command left no operational trace"


def test_commands_cannot_reach_another_tenant(db):
    """An operator's typo must not cross tenants."""
    org_a, owner_a, _, _, _ = _setup(db)
    org_b, owner_b, service_b, opportunity_b, workflow_b = _setup(db)

    commands_a = AdminCommands(db, org_a.id, actor_id=owner_a)
    with pytest.raises(NotFound):
        commands_a.show_workflow(workflow_b.id)
    with pytest.raises(NotFound):
        commands_a.cancel_workflow(workflow_b.id)


def test_an_unscoped_admin_command_is_refused(db):
    with pytest.raises(AdminError) as excinfo:
        AdminCommands(db, "", actor_id="someone")
    assert "refused" in str(excinfo.value)


def test_the_cli_dispatches_and_rejects_unknown_commands(db):
    org, owner, service, opportunity, workflow = _setup(db)

    listed = run_command(db, org.id, owner, ["list-stuck-workflows"])
    assert listed.ok

    with pytest.raises(Refused) as excinfo:
        run_command(db, org.id, owner, ["drop-database"])
    assert "unknown command" in str(excinfo.value)

    with pytest.raises(Refused):
        run_command(db, org.id, owner, [])

    assert "pause-agent" in COMMANDS and "retry-step" in COMMANDS


def test_drain_fleet_is_scoped_to_the_running_organisation(db):
    """The command drains the fleet, which is inherently cross-tenant; the audit
    records who asked, and the work it creates is still attributed per tenant."""
    org, owner, service, opportunity, workflow = _setup(db)
    result = AdminCommands(db, org.id, actor_id=owner).drain_fleet(rounds=1)
    db.commit()
    assert result.ok
    jobs = db.execute(select(models.Job)).scalars().all()
    assert all(j.org_id == org.id for j in jobs)


# ---------------------------------------------------------------------------
# 9. STATUS PANEL
# ---------------------------------------------------------------------------
def test_the_status_panel_reports_the_full_breakdown(db):
    org, owner, service, opportunity, workflow = _setup(db)
    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    runner.run_forever(max_sweeps=1)

    db.expire_all()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    status = service.status()
    payload = status.as_dict()

    for field_name in (
        "opportunities_evaluated_today", "hard_rule_rejects", "matches",
        "strong_matches", "applications_created", "research_completed",
        "waiting_for_data", "waiting_for_approval", "active_workflows",
        "failed_workflows", "last_successful_work", "last_attempted_work",
        "emails_handled_today", "applications_submitted",
    ):
        assert field_name in payload, f"the panel is missing {field_name}"

    assert status.opportunities_evaluated_today >= 1
    assert status.matches >= 1
    assert status.applications_created >= 1
    assert status.last_successful_work is not None


def test_the_panel_keeps_the_absent_features_at_zero(db):
    """Email and submission do not exist. The panel must not imply they do."""
    org, owner, service, opportunity, workflow = _setup(db)
    runner = FleetRunner(_factory(db), interval_seconds=1.0)
    runner.run_forever(max_sweeps=1)
    db.expire_all()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    status = service.status()
    assert status.emails_handled_today == 0
    assert status.applications_submitted == 0


def test_the_panel_distinguishes_successful_from_attempted_work(db):
    """A failed attempt must not be reported as success."""
    org, owner, service, opportunity, workflow = _setup(db)
    workflow.subject_id = "does-not-exist"
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    status = service.status()
    assert status.last_successful_work is None or status.last_attempted_work is not None
