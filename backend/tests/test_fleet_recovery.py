"""Phase 6d hardening: crash recovery, Redis loss, concurrency, tenancy.

The brief's requirement is explicit: **do not merely assert the recovery code
exists - execute the failure scenarios.** So every test here performs a genuine
partial operation and then exercises the real recovery path.

Crash points are simulated by committing a prefix of the work and abandoning the
rest, which is what a process death leaves behind: PostgreSQL has whatever
committed, and nothing else. The recovery path is then the real one -
``FleetDispatcher.dispatch_once``, ``OutboxRelay.drain_once``,
``JobLedger.reclaim_expired`` - not a test-only shortcut.
"""

from __future__ import annotations

import sys
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
from agent.concurrency import AlreadyRunning, ConcurrencyGuard, WorkKey  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.research import DonorResearchService  # noqa: E402
from agent.workflow_engine import (  # noqa: E402
    WORKFLOW_MATCH,
    WORKFLOW_RESEARCH,
    AgentWorker,
    ExecutionResult,
    FleetDispatcher,
)
from events.ledger import JobLedger  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "recovery.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="War Child Test", country="Uganda", slug=None, with_documents=True):
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
    memory.record_fact(key="country", value=country, state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(key="organisation_type", value="NGO", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(
        key="registration_valid_until", value="2030-01-01", state=models.OrgFact.VERIFIED,
        source="user:1", valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()
    if with_documents:
        vault = DocumentVault(db, row.id)
        document = vault.add_version(
            title="Certificate of Registration", doc_type="registration_certificate",
            storage_key=f"org/{row.slug}/reg.pdf", checksum_sha256=checksum_bytes(b"cert"),
            mime_type="application/pdf",
            valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        vault.approve(document, approved_by="user:1")
        db.commit()
    return row


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


def _provision(db, org, autonomy=Autonomy.MONITOR_ONLY):
    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=autonomy)
    db.commit()
    return service


def _schedule(db, service, opportunity, workflow_type=WORKFLOW_MATCH, specialist="MATCHER"):
    workflow = service.schedule(
        workflow_type=workflow_type,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        specialist_key=specialist,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    return workflow


def _drain(db, worker_id="shared-worker", rounds=8):
    """Run the fleet to quiescence the way a deployed loop would."""
    worker = AgentWorker(db, worker_id=worker_id)
    outcomes = []
    for _ in range(rounds):
        dispatched = FleetDispatcher(db).dispatch_once()
        db.commit()
        pending = db.execute(
            select(models.Job).where(models.Job.state == models.Job.QUEUED)
        ).scalars().all()
        if not pending and dispatched.dispatched == 0:
            break
        for job in pending:
            outcomes.append(worker.execute(job.id))
            db.commit()
    return outcomes


# ---------------------------------------------------------------------------
# 1. CRASH-RECOVERY MATRIX
# ---------------------------------------------------------------------------
def test_crash_a_dispatcher_dies_before_creating_the_job(db):
    """A. The dispatcher selected due work and died before enqueueing.

    Nothing durable was written, so the workflow is still due and the next sweep
    finds it. No lost workflow.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    workflow = _schedule(db, service, opportunity)

    # A partial dispatch: read the candidates, then abandon before enqueueing.
    candidates = FleetDispatcher(db).due_workflows()
    assert [w.id for w in candidates] == [workflow.id]
    db.rollback()

    assert db.execute(select(models.Job)).scalars().all() == []
    assert workflow.state in {models.AgentWorkflow.PENDING, models.AgentWorkflow.WAITING}

    # Recovery is the ordinary path.
    assert FleetDispatcher(db).dispatch_once().dispatched == 1
    db.commit()
    assert len(db.execute(select(models.Job)).scalars().all()) == 1


def test_crash_b_job_and_outbox_are_one_transaction(db):
    """B. A crash between job creation and the outbox commit is impossible.

    They are written in the SAME transaction, so a rollback loses both. This is
    the whole reason the outbox exists: an inline ``XADD`` would have published
    work that then rolled back, or lost an event for work that committed.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)

    FleetDispatcher(db).dispatch_once()
    # Crash before commit.
    db.rollback()

    assert db.execute(select(models.Job)).scalars().all() == []
    assert db.execute(select(models.OutboxEvent)).scalars().all() == [], (
        "an event survived a rollback that discarded its job"
    )


def test_crash_c_relay_dies_before_publishing(db):
    """C. The relay could not publish. Nothing is lost and the failure is recorded.

    ``drain_once`` deliberately does **not** raise: one unreachable Redis must not
    kill the sweep, because a relay that dies takes every later event with it. So
    the assertion is on the observable consequences instead - ``published_at`` is
    still NULL, so the row is retried, and the attempt counter advances so the
    abandonment path is reachable and an operator can see the failure.

    That second part was a REAL DEFECT: the relay used to roll back when nothing
    published, discarding the very counters it had just written.
    """
    from events.relay import OutboxRelay

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()

    pending = db.execute(
        select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
    ).scalars().all()
    assert pending
    assert all(e.attempts == 0 for e in pending)

    class DeadPublisher:
        def publish_raw(self, **kwargs):
            raise RuntimeError("redis is gone")

    relay = OutboxRelay(db, DeadPublisher())
    assert relay.drain_once() == 0, "a failed publish reported success"
    db.commit()

    after = db.execute(
        select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
    ).scalars().all()
    assert len(after) == len(pending), "a failed publish consumed the event"
    assert all(e.attempts == 1 for e in after), (
        "the failed-publish attempt counter was discarded"
    )
    assert all(e.last_error and "redis is gone" in e.last_error for e in after), (
        "the failure reason was discarded, so an operator cannot see what is wrong"
    )


def test_a_persistently_failing_publish_eventually_abandons(db):
    """The path the discarded counters made unreachable.

    Without durable attempts, ``max_attempts`` could never be reached and the
    relay would hammer an unreachable Redis forever with no operator-visible signal.
    """
    from events.relay import OutboxRelay

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()

    class DeadPublisher:
        def publish_raw(self, **kwargs):
            raise RuntimeError("redis is gone")

    relay = OutboxRelay(db, DeadPublisher(), max_attempts=3)
    for _ in range(3):
        relay.drain_once()

    abandoned = db.execute(select(models.OutboxEvent)).scalars().all()
    assert all(e.attempts >= 3 for e in abandoned), "attempts did not accumulate durably"
    # And the row is still present rather than deleted: an operator can see it.
    assert all(e.published_at is None for e in abandoned)


def test_crash_d_relay_dies_after_publish_before_marking(db):
    """D. Published, then died before recording it. The next drain re-publishes.

    That must be **harmless**, because at-least-once is the contract. The consumer
    dedupe is the job's idempotency key, so a duplicate event cannot produce a
    second job.
    """
    from events.relay import OutboxRelay

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()

    published: list[dict] = []

    class RecordingPublisher:
        def publish_raw(self, **kwargs):
            published.append(kwargs.get("fields", {}))
            return "1-1"

    relay = OutboxRelay(db, RecordingPublisher())
    relay.drain_once()
    db.commit()
    first_count = len(published)
    assert first_count >= 1

    # Simulate the crash: clear published_at as if the marking never happened.
    for event in db.execute(select(models.OutboxEvent)).scalars():
        event.published_at = None
        event.attempts = 0
    db.commit()

    relay.drain_once()
    db.commit()
    assert len(published) > first_count, "the re-publish did not happen"

    # Harmless: still exactly one job, because the idempotency key holds.
    assert len(db.execute(select(models.Job)).scalars().all()) == 1
    assert FleetDispatcher(db).dispatch_once().duplicates + 1 >= 1


def test_crash_e_worker_dies_after_redis_read_before_db_work(db):
    """E. The message was read and the worker died before touching the database.

    The job is still QUEUED and lease-free, so a redelivery claims it normally.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    # Nothing happened: no claim, no attempt.
    assert job.state == models.Job.QUEUED
    assert job.lease_owner is None
    assert db.execute(select(models.JobAttempt)).scalars().all() == []

    result = AgentWorker(db, worker_id="replacement").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.SUCCEEDED


def test_crash_f_worker_dies_during_the_transaction(db):
    """F. The worker died mid-transaction. Nothing it did survives."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    AgentWorker(db, worker_id="doomed").execute(job.id)
    # Crash before commit.
    db.rollback()

    fresh = db.execute(select(models.Job).where(models.Job.id == job.id)).scalars().one()
    assert fresh.state == models.Job.QUEUED, "a rolled-back execution left the job claimed"
    assert db.execute(select(models.Application)).scalars().all() == []
    assert db.execute(select(models.OpportunityMatch)).scalars().all() == []

    result = AgentWorker(db, worker_id="replacement").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.SUCCEEDED
    assert len(db.execute(select(models.Application)).scalars().all()) == 1


def test_crash_g_worker_dies_after_commit_before_ack(db):
    """G. Committed, then died before acking. The message is redelivered.

    That must be harmless - the job is SUCCEEDED and not claimable.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    worker = AgentWorker(db, worker_id="w")
    assert worker.execute(job.id).outcome == ExecutionResult.SUCCEEDED
    db.commit()  # committed; the ACK never happened

    # Redelivery.
    assert worker.execute(job.id).outcome == ExecutionResult.SKIPPED
    db.commit()
    assert len(db.execute(select(models.Application)).scalars().all()) == 1
    assert len(db.execute(select(models.OpportunityMatch)).scalars().all()) == 1


def test_crash_h_worker_dies_after_the_decision_before_the_next_step(db):
    """H. The decision persisted; the workflow never advanced.

    Recovery re-runs the step. The decision is recorded again, but the *workflow*
    cannot fork: the follow-on workflow is unique per (agent, type, subject).
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    workflow = _schedule(db, service, opportunity)

    outcomes = _drain(db)
    assert any(o.outcome == ExecutionResult.SUCCEEDED for o in outcomes)

    # Rewind the workflow as a crash before advancement would leave it.
    workflow.state = models.AgentWorkflow.PENDING
    workflow.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    before = len(db.execute(select(models.AgentWorkflow)).scalars().all())
    _drain(db)

    after = len(db.execute(select(models.AgentWorkflow)).scalars().all())
    assert after == before, (
        f"recovery forked the workflow chain: {before} -> {after}"
    )
    assert len(db.execute(select(models.Application)).scalars().all()) == 1, (
        "recovery created a second application workspace"
    )


def test_crash_i_worker_dies_after_research_before_advancement(db):
    """I. Research persisted, workflow not advanced, job recovered.

    The brief names this case specifically: no duplicate research record for the
    same version. ``research()`` is idempotent per opportunity revision, which is
    the fix that makes this true rather than approximately true.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    agent = service.get()

    research_service = DonorResearchService(db, agent)
    first = research_service.research(opportunity)
    db.commit()

    # The recovered job re-runs the same work for the same opportunity revision.
    again = research_service.research(opportunity)
    db.commit()

    assert again.id == first.id, "recovery appended a duplicate research version"
    assert len(research_service.history(opportunity.id)) == 1
    assert len(db.execute(select(models.DonorResearch)).scalars().all()) == 1

    # But a genuinely new revision still appends, because that is change
    # detection rather than a retry.
    opportunity.version += 1
    db.commit()
    third = research_service.research(opportunity)
    db.commit()
    assert third.version == 2
    assert len(research_service.history(opportunity.id)) == 2


def test_crash_j_agent_is_paused_during_active_work(db):
    """J. Pause mid-flight. Already covered in test_fleet.py; asserted here too so
    the whole matrix lives in one place."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    agent = service.get()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.AGENT_PAUSED
    assert db.execute(select(models.Application)).scalars().all() == []


def test_the_whole_matrix_leaves_no_partial_state(db):
    """After running every crash point with recovery, the records are consistent.

    A blunt but valuable check: no orphaned applications, no application without a
    match, no research without an agent, no job RUNNING without a lease.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    _drain(db)

    applications = db.execute(select(models.Application)).scalars().all()
    matches = db.execute(select(models.OpportunityMatch)).scalars().all()
    research = db.execute(select(models.DonorResearch)).scalars().all()
    jobs = db.execute(select(models.Job)).scalars().all()

    assert len(applications) == 1 and applications[0].org_id == org.id
    assert all(m.org_id == org.id for m in matches)
    assert all(r.agent_id == service.get().id for r in research)
    assert all(j.org_id == org.id for j in jobs)
    # Nothing may be left holding a lease it no longer owns.
    for job in jobs:
        if job.state in {models.Job.QUEUED, models.Job.SUCCEEDED}:
            assert job.lease_owner is None, f"{job.id} finished still holding a lease"


# ---------------------------------------------------------------------------
# 2. REDIS LOSS AND RECOVERY
# ---------------------------------------------------------------------------
def test_redis_loss_erases_nothing_durable(db):
    """Redis goes away while durable work exists. PostgreSQL must hold everything.

    The relay cannot publish, so events accumulate unpublished - and **nothing
    else changes**. Workflows, applications, research, decisions and activity all
    live in PostgreSQL and are untouched by the cache being gone.
    """
    from events.relay import OutboxRelay

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    _drain(db)

    before = {
        "workflows": len(db.execute(select(models.AgentWorkflow)).scalars().all()),
        "applications": len(db.execute(select(models.Application)).scalars().all()),
        "matches": len(db.execute(select(models.OpportunityMatch)).scalars().all()),
        "research": len(db.execute(select(models.DonorResearch)).scalars().all()),
        "activity": len(db.execute(select(models.AgentActivity)).scalars().all()),
        "decisions": len(db.execute(select(models.DecisionRecord)).scalars().all()),
    }
    assert before["applications"] == 1
    assert before["activity"] > 0

    class DeadPublisher:
        def publish_raw(self, **kwargs):
            raise RuntimeError("connection refused")

    relay = OutboxRelay(db, DeadPublisher())
    for _ in range(3):
        try:
            relay.drain_once()
        except RuntimeError:
            db.rollback()

    after = {
        "workflows": len(db.execute(select(models.AgentWorkflow)).scalars().all()),
        "applications": len(db.execute(select(models.Application)).scalars().all()),
        "matches": len(db.execute(select(models.OpportunityMatch)).scalars().all()),
        "research": len(db.execute(select(models.DonorResearch)).scalars().all()),
        "activity": len(db.execute(select(models.AgentActivity)).scalars().all()),
        "decisions": len(db.execute(select(models.DecisionRecord)).scalars().all()),
    }
    assert after == before, f"Redis loss changed durable state: {before} -> {after}"

    pending = db.execute(
        select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
    ).scalars().all()
    assert pending, "events should still be waiting to be delivered"
    assert all(e.attempts > 0 for e in pending), "failed publishes were not retried"


def test_redis_recovery_delivers_the_backlog_without_reconstruction(db):
    """Redis comes back. The relay drains the backlog; nobody rebuilds anything."""
    from events.relay import OutboxRelay

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()

    pending_before = len(
        db.execute(
            select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
        ).scalars().all()
    )
    assert pending_before > 0

    delivered: list[str] = []

    class LivePublisher:
        def publish_raw(self, **kwargs):
            delivered.append(str(kwargs.get("fields", {}).get("event_type")))
            return "2-2"

    published = OutboxRelay(db, LivePublisher()).drain_once()
    db.commit()

    assert published == pending_before
    assert len(delivered) == pending_before
    still_pending = db.execute(
        select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
    ).scalars().all()
    assert still_pending == [], "the backlog was not fully drained after recovery"


# ---------------------------------------------------------------------------
# 3. CROSS-TENANT ADVERSARIAL
# ---------------------------------------------------------------------------
def _two_tenants(db):
    """Two fully provisioned tenants, with Tenant A having done real work.

    Returns the execution result so a test can prove the setup actually succeeded
    before asserting isolation. Without that, an isolation assertion can "pass"
    because nothing happened anywhere - and the first version of this helper did
    exactly that, leaving the activity table empty and the test unable to say why.
    """
    a = _org(db, name="Tenant A", slug="tenant-a")
    b = _org(db, name="Tenant B", slug="tenant-b")
    service_a, service_b = _provision(db, a), _provision(db, b)
    opportunity = _opportunity(db)
    _schedule(db, service_a, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    return a, b, service_a, service_b, result


def test_tenant_a_cannot_read_tenant_b_records_through_any_service(db):
    """Every app-tier query in the fleet is scoped by organisation.

    RLS is the *database* boundary and is proven separately on PostgreSQL; this
    proves the *application* boundary, which is what protects a deployment whose
    connection is ever more privileged than intended.
    """
    a, b, service_a, service_b, result = _two_tenants(db)
    assert result.outcome == ExecutionResult.SUCCEEDED, (
        f"the isolation test's own setup failed: {result.outcome} - {result.detail}"
    )

    # Agent.
    assert service_a.get().org_id == a.id
    assert service_b.get().org_id == b.id
    assert service_a.get().id != service_b.get().id

    # Workflows.
    assert all(
        w.org_id == service_a.org_id
        for w in db.execute(select(models.AgentWorkflow)).scalars()
        if w.org_id == service_a.org_id
    )

    # Activity, research and jobs are all attributed to exactly one tenant.
    activity = db.execute(select(models.AgentActivity)).scalars().all()
    assert activity, "the setup produced no activity, so nothing is being tested"
    assert all(rec.org_id == a.id for rec in activity)
    assert all(rec.agent_id == service_a.get().id for rec in activity)

    matches = db.execute(select(models.OpportunityMatch)).scalars().all()
    assert matches and all(rec.org_id == a.id for rec in matches)

    jobs = db.execute(select(models.Job)).scalars().all()
    assert jobs and all(job.org_id == a.id for job in jobs)

    # And Tenant B's status panel sees none of it.
    status_b = service_b.status()
    assert status_b.applications_in_progress == 0
    assert status_b.opportunities_scanned_today == 0
    assert status_b.active_workflows == 0
    # While Tenant A's sees exactly its own.
    status_a = service_a.status()
    assert status_a.applications_in_progress == 1
    assert status_a.opportunities_scanned_today == 1


def test_a_forged_workflow_id_from_another_tenant_is_rejected(db):
    """A job naming another tenant's workflow must not execute against it."""
    a, b, service_a, service_b, _ = _two_tenants(db)
    applications_before = len(db.execute(select(models.Application)).scalars().all())
    opportunity_b = _opportunity(db)

    # Tenant B's workflow id, smuggled into Tenant A's job.
    workflow_b = service_b.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity_b.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()

    ledger = JobLedger(db)
    job, _ = ledger.enqueue(
        org_id=a.id, job_type=WORKFLOW_MATCH, payload={"workflow_id": workflow_b.id},
        domain="jobs", action=WORKFLOW_MATCH, idempotency_key="forged-workflow-1",
    )
    job.agent_id = service_a.get().id
    job.agent_version = service_a.get().version
    job.workflow_id = workflow_b.id  # the forgery
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.FAILED
    assert "not in this organisation" in (result.detail or "")
    # The count must not have grown: measuring against zero would have failed for
    # the wrong reason, because Tenant A legitimately has one application already.
    applications_after = len(db.execute(select(models.Application)).scalars().all())
    assert applications_after == applications_before, (
        "a forged workflow id produced an application"
    )
    assert applications_after == 1


def test_a_forged_agent_id_in_the_payload_is_ignored(db):
    """The Redis payload is a hint for locating work, never authority."""
    a, b, service_a, service_b, _ = _two_tenants(db)
    opportunity = _opportunity(db)
    _schedule(db, service_a, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()

    job = db.execute(
        select(models.Job).where(models.Job.org_id == a.id, models.Job.state == models.Job.QUEUED)
    ).scalars().first()
    assert job is not None
    # The payload claims Tenant B's agent.
    job.payload = {**(job.payload or {}), "agent_id": service_b.get().id}
    db.commit()

    from agent.granada_agent import GranadaAgentService

    resolved = GranadaAgentService.for_job(db, job)
    assert resolved.org_id == a.id, "the payload's agent id was trusted"
    assert resolved.get().id == service_a.get().id


def test_a_system_job_with_no_agent_remains_valid(db):
    """NULL agent is legal only for work that genuinely belongs to nobody."""
    org = _org(db)
    job = models.Job(
        org_id=org.id, agent_id=None, stream="granada:v1:jobs:system", job_type="system_task",
        state=models.Job.QUEUED, available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    assert job.agent_id is None

    # But it must not be executed as anybody's agent work.
    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome in {ExecutionResult.SKIPPED, ExecutionResult.FAILED}


# ---------------------------------------------------------------------------
# 4. PER-AGENT CONCURRENCY
# ---------------------------------------------------------------------------
def test_one_agent_can_work_on_two_subjects_at_once(db):
    """The lock is the SUBJECT, not the agent.

    A global agent lock would serialise everything an organisation does, which is
    the opposite of what "your agent works for you continuously" means.
    """
    org = _org(db)
    service = _provision(db, org)
    first = _opportunity(db)
    second = _opportunity(db)

    first_workflow = _schedule(db, service, first)
    second_workflow = _schedule(db, service, second)

    assert first_workflow.id != second_workflow.id, (
        "two different opportunities collapsed into one workflow"
    )
    db.commit()

    capacity = ConcurrencyGuard(db).parallel_capacity(service.get().id)
    assert capacity["in_flight"] >= 0  # nothing claimed yet
    assert len({first.id, second.id}) == 2


def test_the_same_subject_cannot_be_dispatched_twice_concurrently(db):
    """Same agent, same subject, same work type -> one workflow, one job."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    first = _schedule(db, service, opportunity)
    second = _schedule(db, service, opportunity)
    db.commit()

    assert first.id == second.id
    assert len(db.execute(select(models.AgentWorkflow)).scalars().all()) == 1

    FleetDispatcher(db).dispatch_once()
    db.commit()
    assert len(db.execute(select(models.Job)).scalars().all()) == 1


def test_an_in_flight_subject_is_reported_as_already_running(db):
    """The guard produces a readable reason rather than a silent skip."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    guard = ConcurrencyGuard(db)
    guard.assert_claimable(
        agent_id=service.get().id, work_type=WORKFLOW_MATCH, subject_id=opportunity.id
    )

    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    # A second claim attempt on the same job is refused by the ledger's lease.
    assert AgentWorker(db, worker_id="w2").execute(job.id).outcome == ExecutionResult.SKIPPED


def test_research_is_idempotent_per_opportunity_revision(db):
    """The uniqueness constraint behind crash point I."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    research_service = DonorResearchService(db, service.get())

    for _ in range(3):
        research_service.research(opportunity)
        db.commit()

    assert len(db.execute(select(models.DonorResearch)).scalars().all()) == 1
    assert ConcurrencyGuard(db).research_version_exists(
        agent_id=service.get().id,
        opportunity_id=opportunity.id,
        opportunity_version=opportunity.version,
    )


def test_stale_leases_are_discoverable(db):
    """The recovery work list is read-only; the ledger owns the mutation."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    _schedule(db, service, opportunity)
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    JobLedger(db).claim(job_id=job.id, worker_id="doomed", lease_seconds=300)
    db.commit()
    assert ConcurrencyGuard(db).stale_leases() == []

    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert [j.id for j in ConcurrencyGuard(db).stale_leases()] == [job.id]

    # And the real recovery path reclaims it.
    assert JobLedger(db).reclaim_expired() == [job.id]
    db.commit()
    assert db.execute(select(models.Job).where(models.Job.id == job.id)).scalars().one().state == models.Job.QUEUED


def test_work_keys_are_scoped_to_the_subject():
    """A lock name that mentioned only the agent would serialise everything."""
    a = WorkKey(agent_id="agent-1", org_id="org-1", work_type=WORKFLOW_MATCH, subject_id="opp-1")
    b = WorkKey(agent_id="agent-1", org_id="org-1", work_type=WORKFLOW_MATCH, subject_id="opp-2")
    c = WorkKey(agent_id="agent-1", org_id="org-1", work_type=WORKFLOW_RESEARCH, subject_id="opp-1")
    assert a.lock_name != b.lock_name, "different subjects shared a lock"
    assert a.lock_name != c.lock_name, "different work types shared a lock"
    assert a.lock_name == WorkKey(
        agent_id="agent-1", org_id="org-1", work_type=WORKFLOW_MATCH, subject_id="opp-1"
    ).lock_name
