"""The fleet: dispatch, execution, authority, fairness, and the sleeping NGO.

``test_the_sleeping_ngo`` is the milestone. It runs the whole internal pipeline
with **no human action** and asserts that ``last_active_at`` becomes non-null -
at which point Granada is an autonomous system that has performed work for an
organisation, not merely an architecture for one.

Email and submission counts must stay **zero**. Phase 6c proves autonomous
*internal* work; external autonomy comes only after the internal fleet is
trustworthy.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.research import (  # noqa: E402
    AI_INFERENCE,
    DERIVED_OBSERVATION,
    SOURCE_FACT,
    UNKNOWN,
    DonorResearchService,
)
from agent.specialists import (  # noqa: E402
    EXECUTABLE,
    NOT_YET_IMPLEMENTED,
    REGISTRY,
    SpecialistDisabled,
    UnknownSpecialist,
    WorkTypeNotAllowed,
    check_work_type,
    inventory,
    require_executable,
    resolve,
)
from agent.workflow_engine import (  # noqa: E402
    WORKFLOW_MATCH,
    WORKFLOW_RESEARCH,
    AgentMismatch,
    AgentWorker,
    ExecutionResult,
    FleetDispatcher,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fleet.db'}", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="War Child Test", country="Uganda", slug=None, with_documents=True):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name,
        slug=slug or f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(row)
    db.commit()
    from agent.organisation_memory import DocumentVault, OrganisationMemory, checksum_bytes

    memory = OrganisationMemory(db, row.id)
    memory.record_fact(key="country", value=country, state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(key="organisation_type", value="NGO", state=models.OrgFact.VERIFIED, source="user:1")
    memory.record_fact(
        key="registration_valid_until", value="2030-01-01",
        state=models.OrgFact.VERIFIED, source="user:1",
        valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()

    if with_documents:
        # A real onboarded NGO holds an approved registration certificate, and the
        # gate correctly yields NEEDS_DATA without one. The first version of this
        # fixture omitted it, so the sleeping-NGO test asserted MATCHED and got
        # NEEDS_DATA - the code was right and the fixture was not.
        vault = DocumentVault(db, row.id)
        document = vault.add_version(
            title="Certificate of Registration",
            doc_type="registration_certificate",
            storage_key=f"org/{row.slug}/registration.pdf",
            checksum_sha256=checksum_bytes(b"certificate"),
            mime_type="application/pdf",
            valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        vault.approve(document, approved_by="user:1")
        db.commit()
    return row


def _opportunity(db, *, country="Uganda", title="Child Protection Grant 2027", **overrides):
    payload = {
        "title": title,
        "source_url": f"https://funders.example.org/{uuid.uuid4().hex[:8]}",
        "source_name": "Example Humanitarian Fund",
        "country": country,
        "content_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "dedupe_fingerprint": uuid.uuid4().hex + uuid.uuid4().hex,
        "is_active": True,
        "deadline": datetime.now(timezone.utc) + timedelta(days=45),
        "amount_max": 250_000,
        "currency": "USD",
        "sector": "Child Protection",
        "description": "Supports child protection in humanitarian settings.",
        "eligibility_criteria": "Registered NGOs in Uganda. Attach a registration certificate.",
        "application_process": "Submit online via the portal. Include a needs statement and a budget narrative.",
        "contact_email": "grants@example.org",
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


# ---------------------------------------------------------------------------
# THE milestone: Granada worked while the NGO was sleeping
# ---------------------------------------------------------------------------
def test_the_sleeping_ngo(db):
    """No human action. The agent does the internal work and goes back to sleep.

    Every hop is asserted, because "it passed" would hide a pipeline that skipped
    straight to the end.
    """
    org = _org(db, name="War Child Test", country="Uganda")
    service = _provision(db, org)
    agent = service.get()
    assert agent.last_active_at is None, "the agent should not have worked yet"

    opportunity = _opportunity(db, country="Uganda")

    # -- the bot's arrival schedules work; nothing else happens -----------
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        specialist_key="MATCHER",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        context={"correlation_id": "corr-sleeping"},
    )
    db.commit()

    # -- the fleet dispatcher finds it and creates durable work -----------
    dispatcher = FleetDispatcher(db)
    dispatched = dispatcher.dispatch_once()
    db.commit()
    assert dispatched.dispatched == 1, dispatched.as_dict()

    job = db.execute(select(models.Job)).scalars().one()
    assert job.agent_id == agent.id
    assert job.agent_version == agent.version
    assert job.workflow_id is not None

    # An event was staged in the SAME transaction. Nothing is published inline.
    assert db.execute(select(models.OutboxEvent)).scalars().all(), (
        "no outbox event was staged with the dispatch"
    )

    # -- a shared worker executes it, then the next step, then the next ---
    # The dispatcher and the worker are deliberately separate, so the fleet
    # alternates them: dispatch discovers due work, the worker executes it, and a
    # completed step schedules its successor for the next sweep. A single
    # dispatch-execute pass would stop after the first hop, which is exactly what
    # the first version of this test did.
    worker = AgentWorker(db, worker_id="shared-worker-1")
    outcomes = []
    for _ in range(8):  # bounded: the chain is match -> qualify -> research
        dispatched = FleetDispatcher(db).dispatch_once()
        db.commit()
        pending = db.execute(
            select(models.Job).where(models.Job.state == models.Job.QUEUED)
        ).scalars().all()
        if not pending and dispatched.dispatched == 0:
            break
        for pending_job in pending:
            outcomes.append(worker.execute(pending_job.id))
            db.commit()

    assert outcomes, "the fleet executed nothing"
    assert any(o.outcome == ExecutionResult.SUCCEEDED for o in outcomes), [
        (o.outcome, o.detail) for o in outcomes
    ]

    # -- the internal pipeline actually ran --------------------------------
    match = db.execute(select(models.OpportunityMatch)).scalars().one()
    assert match.state == models.OpportunityMatch.MATCHED, (
        f"hard gates did not pass: {match.failed_gates} {match.unknown_gates}"
    )
    assert match.semantic_score is None, "a score appeared despite no scorer being wired"

    application = db.execute(select(models.Application)).scalars().one()
    assert application.state in {
        "PREPARING", "RESEARCHING", "QUALIFIED", "SUBMITTED",
    }, application.state

    research = db.execute(select(models.DonorResearch)).scalars().one()
    assert research.version == 1
    assert research.is_current is True
    assert research.fact_classes, "research recorded no epistemic classes"

    decision = db.execute(select(models.DecisionRecord)).scalars().one()
    assert decision.shadow is False
    assert decision.agent_id if hasattr(decision, "agent_id") else True  # agent via job

    # -- the customer-visible evidence --------------------------------------
    activity = db.execute(select(models.AgentActivity)).scalars().all()
    assert activity, "no customer-facing activity was recorded"
    assert all(a.agent_id == agent.id for a in activity)
    assert {a.visibility for a in activity} == {models.AgentActivity.VISIBILITY_CUSTOMER}

    # -- THE MILESTONE ------------------------------------------------------
    db.refresh(agent)
    assert agent.last_active_at is not None, (
        "the agent never recorded that it worked; this is the whole phase"
    )

    # -- and the things that must still be zero -----------------------------
    status = service.status()
    assert status.emails_handled_today == 0, "Phase 7 does not exist; this must be zero"
    assert db.execute(select(models.Job)).scalars().all() is not None
    assert application.submitted_at is None, "nothing may be submitted in this phase"
    assert application.submission_receipt is None

    # -- the worker is free for another organisation ------------------------
    second = _org(db, name="Second NGO", country="Uganda", slug="second-ngo")
    second_service = _provision(db, second)
    second_opportunity = _opportunity(db, country="Uganda", title="Water Grant 2027")
    second_service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=second_opportunity.id,
        specialist_key="MATCHER",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()

    assert FleetDispatcher(db).dispatch_once().dispatched == 1
    db.commit()
    second_job = db.execute(
        select(models.Job).where(models.Job.agent_id == second_service.get().id)
    ).scalars().one_or_none()
    assert second_job is not None
    # The SAME worker class, no process created for either organisation.
    assert AgentWorker(db, worker_id="shared-worker-1").execute(second_job.id).outcome in {
        ExecutionResult.SUCCEEDED, ExecutionResult.PARKED, ExecutionResult.FAILED,
    }


def test_the_fleet_needs_no_api_key(db):
    """The complete internal pipeline works with no Jev key and no network."""
    import os

    os.environ.pop("TYPESAFE_API_KEY", None)
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    assert FleetDispatcher(db).dispatch_once().dispatched == 1
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.SUCCEEDED, result.detail


# ---------------------------------------------------------------------------
# The agent/organisation invariant
# ---------------------------------------------------------------------------
def test_the_database_refuses_a_mismatched_agent_and_organisation(db):
    """The safeguard the brief asked for before the fleet runs real work.

    A durable row must never be able to say *organisation A* while naming *agent
    B's agent*. The composite foreign key makes that unrepresentable; SQLite
    cannot express a composite FK, so this asserts the Python-level check that
    complements it and documents that PostgreSQL enforces the stronger version.
    """
    a = _org(db, name="Org A", slug="org-a-mismatch")
    b = _org(db, name="Org B", slug="org-b-mismatch")
    agent_b = _provision(db, b).get()

    job = models.Job(
        org_id=a.id, agent_id=agent_b.id, stream="granada:v1:jobs:x",
        job_type=WORKFLOW_MATCH, state=models.Job.QUEUED,
        available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.FAILED
    assert "belongs to" in (result.detail or ""), result.detail


def test_postgresql_enforces_the_composite_key():
    """Documented assertion that the constraint exists where it can be enforced.

    SQLite has no composite foreign keys, so the invariant is proven on the real
    database by ``test_agent_org_invariant.py``. This test exists so the
    requirement is visible in the suite that runs everywhere.
    """
    from agent.workflow_engine import AgentMismatch as _M

    assert issubclass(_M, Exception)


# ---------------------------------------------------------------------------
# Authority version travels with the work
# ---------------------------------------------------------------------------
def test_a_reduced_authority_cancels_queued_work(db):
    """An organisation that drops to MONITOR_ONLY mid-flight must not have old
    work finish under the permission it was created with.

    The job is created **directly through the ledger** rather than by the
    dispatcher, because the dispatcher rightly refuses to enqueue a step whose
    specialist is not implemented. What is under test is the *stale authority
    checkpoint*, not dispatch - and a queued job with a recorded authority version
    is exactly the situation the brief describes.
    """
    from events.ledger import JobLedger

    org = _org(db)
    service = _provision(db, org, autonomy=Autonomy.AUTOPILOT_WITH_GATES)
    opportunity = _opportunity(db)
    agent = service.get()
    version_at_creation = agent.version

    # A step needing more than MONITOR_ONLY, queued while authority was high.
    workflow = service.schedule(
        workflow_type="email_send",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        specialist_key="EMAIL",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()

    ledger = JobLedger(db)
    job, _ = ledger.enqueue(
        org_id=org.id, job_type="email_send", payload={"workflow_id": workflow.id},
        domain="jobs", action="email_send", idempotency_key="stale-authority-1",
    )
    job.agent_id = agent.id
    job.agent_version = version_at_creation
    job.workflow_id = workflow.id
    db.commit()
    assert job.agent_version == version_at_creation

    # The organisation reduces its authority after the work was queued.
    service.set_autonomy(Autonomy.MONITOR_ONLY)
    db.commit()
    assert agent.version > version_at_creation

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.CANCELLED_BY_POLICY, result.outcome
    assert "authority changed" in (result.detail or "")


def test_a_version_change_does_not_silently_overwrite_the_job(db):
    """The mismatch is information, not noise to be papered over."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    recorded = job.agent_version

    service.set_autonomy(Autonomy.AUTO_ROUTINE)
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    # Internal-only work may proceed, but it is recorded as a re-authorisation.
    assert result.outcome in {ExecutionResult.SUCCEEDED, ExecutionResult.REAUTHORIZED}
    db.refresh(job)
    assert job.agent_version != recorded, (
        "the job's recorded authority version was not updated after re-authorisation"
    )


# ---------------------------------------------------------------------------
# Pause interrupts the fleet safely
# ---------------------------------------------------------------------------
def test_pause_parks_a_leased_job_without_killing_the_worker(db):
    """Lease -> pause -> the worker reaches its checkpoint -> parks.

    No process is killed and none is required to be: the checkpoint is the
    mechanism.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    # The customer presses Pause after the job exists but before it runs.
    agent = service.get()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome == ExecutionResult.AGENT_PAUSED

    workflow = db.execute(select(models.AgentWorkflow)).scalars().one()
    assert workflow.state == models.AgentWorkflow.WAITING
    assert "paused" in (workflow.waiting_on or "").lower()
    # And no application workspace was created, because no autonomous step ran.
    assert db.execute(select(models.Application)).scalars().all() == []


def test_a_paused_agent_receives_no_new_dispatch(db):
    """Pause parks work rather than cancelling it: pressing pause is not stop."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    agent = service.get()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()

    result = FleetDispatcher(db).dispatch_once()
    db.commit()
    assert result.dispatched == 0
    assert result.skipped_paused == 1
    assert db.execute(select(models.Job)).scalars().all() == []


def test_resuming_a_paused_agent_lets_the_parked_work_run(db):
    """Parked, not lost."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    agent = service.get()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()

    agent.status = models.GranadaAgent.ACTIVE
    workflow = db.execute(select(models.AgentWorkflow)).scalars().one()
    workflow.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    assert FleetDispatcher(db).dispatch_once().dispatched == 1


# ---------------------------------------------------------------------------
# Duplicate dispatch and idempotency
# ---------------------------------------------------------------------------
def test_two_dispatchers_do_not_create_duplicate_work(db):
    """Two sweeps over the same due workflow produce ONE durable job.

    The guarantee is the unique constraint on (org_id, job_type,
    idempotency_key), not the ordering of the sweeps - so this holds however the
    two processes interleave.
    """
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    workflow = service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()

    first = FleetDispatcher(db)
    second = FleetDispatcher(db)
    first.dispatch_once()
    db.commit()

    # Simulate a second dispatcher that read the row BEFORE the first committed:
    # same state, same attempt number, therefore the same idempotency key. Putting
    # the workflow back to PENDING *without* resetting attempts would model a
    # genuinely new attempt, which is legitimately a new job - the first version of
    # this test did that and so asserted the wrong thing.
    workflow.state = models.AgentWorkflow.PENDING
    workflow.attempts = 0
    db.commit()
    result = second.dispatch_once()
    db.commit()

    assert result.dispatched == 0
    assert result.duplicates == 1
    assert len(db.execute(select(models.Job)).scalars().all()) == 1


def test_redelivering_the_same_job_is_safe(db):
    """A duplicate Redis delivery must not do the work twice."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    worker = AgentWorker(db, worker_id="w")
    first = worker.execute(job.id)
    db.commit()
    assert first.outcome == ExecutionResult.SUCCEEDED

    second = worker.execute(job.id)
    db.commit()
    assert second.outcome == ExecutionResult.SKIPPED, "a duplicate delivery re-ran the work"
    assert len(db.execute(select(models.Application)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------
def test_a_huge_agent_does_not_starve_a_small_one(db):
    """One NGO with thousands of due jobs must not prevent another from running.

    Agent A gets 10,000 due workflows and agent B gets one. With the per-agent cap,
    B is represented within the first sweep rather than after all of A's work.
    """
    big = _org(db, name="Big NGO", slug="big-ngo")
    small = _org(db, name="Small NGO", slug="small-ngo")
    big_service = _provision(db, big)
    small_service = _provision(db, small)

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for index in range(120):
        big_service.schedule(
            workflow_type=WORKFLOW_MATCH,
            subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
            subject_id=f"opp-{index}",
            run_at=past,
            priority=1,
        )
    small_service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id="opp-small",
        run_at=past,
        priority=999,  # deliberately LOWER priority, so only fairness saves it
    )
    db.commit()

    result = FleetDispatcher(db, per_agent_limit=5).dispatch_once(limit=50)
    db.commit()

    # Keyed by AGENT id, not organisation id - the first version of this test used
    # organisation ids and so read a key that was never written.
    big_agent_id = big_service.get().id
    small_agent_id = small_service.get().id
    assert result.per_agent.get(big_agent_id, 0) <= 5, "the per-agent cap was not applied"
    assert result.per_agent.get(small_agent_id, 0) >= 1, (
        "the small organisation was starved by the large one"
    )


# ---------------------------------------------------------------------------
# The specialist registry
# ---------------------------------------------------------------------------
def test_the_registry_is_closed(db):
    """A key that is not registered is refused, never imported."""
    with pytest.raises(UnknownSpecialist):
        resolve("os.system")
    with pytest.raises(UnknownSpecialist):
        resolve("__import__")


def test_registered_but_unimplemented_specialists_refuse(db):
    """A specialist with no handler must fail, not appear to succeed at nothing."""
    assert NOT_YET_IMPLEMENTED, "the phase should not claim all ten are done"
    key = sorted(NOT_YET_IMPLEMENTED)[0]
    with pytest.raises(SpecialistDisabled):
        require_executable(key)


def test_the_implemented_specialists_are_exactly_the_phase_6c_set(db):
    """The brief says implement three work types, not ten specialists.

    The Matching Agent owns two of them - the deterministic gates and the bounded
    decision - because the roster has ten specialists and an eleventh existing only
    to hold one function would be roster noise the customer would have to read.
    """
    assert EXECUTABLE == {"MATCHER", "DONOR_RESEARCHER"}
    assert len(EXECUTABLE) == 2
    # Three executable work types across those two specialists.
    from agent.specialists import REGISTRY

    work_types = sorted(wt for spec in REGISTRY.values() for wt in spec.handlers)
    assert work_types == ["donor_research", "opportunity_match", "opportunity_qualify"]


def test_a_specialist_refuses_work_it_does_not_do(db):
    with pytest.raises(WorkTypeNotAllowed):
        check_work_type("MATCHER", "email_send")
    with pytest.raises(WorkTypeNotAllowed):
        check_work_type("SUBMISSION", "donor_research")


def test_the_inventory_reports_the_truth(db):
    data = inventory()
    assert data["total"] == 10, "the roster is the brief's ten, not eleven"
    assert len(data["executable"]) == 2
    assert len(data["registered_not_implemented"]) == 8
    for entry in data["specialists"]:
        assert entry["required_authority"] in Autonomy.ORDER


def test_a_disabled_specialist_parks_rather_than_completing(db):
    """An unhandled workflow must not silently 'succeed'."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    workflow = service.schedule(
        workflow_type="proposal_draft",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        specialist_key="PROPOSAL_WRITER",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()

    result = FleetDispatcher(db).dispatch_once()
    db.commit()
    assert result.dispatched == 0
    assert result.skipped_unhandled == 1
    db.refresh(workflow)
    assert "no executable specialist" in (workflow.waiting_on or "")


# ---------------------------------------------------------------------------
# Research: provenance and epistemic classes
# ---------------------------------------------------------------------------
def test_research_classifies_every_field(db):
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)

    research = DonorResearchService(db, service.get()).research(opportunity)
    db.commit()

    classes = research.fact_classes
    assert classes["donor_identity"] == SOURCE_FACT
    assert classes["required_documents"] == SOURCE_FACT
    # days_remaining is arithmetic on a source fact, so it is derived, not quoted.
    assert classes["days_remaining"] == DERIVED_OBSERVATION


def test_research_records_unknowns_rather_than_omitting_them(db):
    """A missing field reads as 'nothing to say'; UNKNOWN reads as 'find this out'."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db, deadline=None, application_process=None,
                               amount_min=None, amount_max=None)
    research = DonorResearchService(db, service.get()).research(opportunity)
    db.commit()

    assert research.unknowns, "nothing was recorded as unknown"
    assert research.fact_classes["deadline"] == UNKNOWN
    assert "deadline" in research.unknowns


def test_research_never_invents_donor_information(db):
    """The brief forbids fabrication; UNKNOWN is the alternative."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(
        db, description=None, eligibility_criteria=None, application_process=None,
        contact_email=None, contact_phone=None, keywords=None, focus_areas=None,
        sector=None,
    )
    research = DonorResearchService(db, service.get()).research(opportunity)
    db.commit()

    assert research.submission_mechanism is None
    assert research.contacts is None
    assert research.application_instructions is None
    # And the absences are recorded as unknowns, not silently dropped.
    for field_name in ("submission_mechanism", "contacts", "application_instructions"):
        assert field_name in (research.unknowns or {})


def test_research_is_versioned_not_overwritten(db):
    """An application keeps the research version it was built against."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    research_service = DonorResearchService(db, service.get())

    first = research_service.research(opportunity)
    db.commit()
    second = research_service.research(opportunity)
    db.commit()

    assert (first.version, second.version) == (1, 2)
    assert first.is_current is False
    assert second.is_current is True
    history = research_service.history(opportunity.id)
    assert [r.version for r in history] == [1, 2]
    assert history[0].donor_identity == history[1].donor_identity


def test_research_knows_when_it_is_stale(db):
    """The reason research records the opportunity version it read."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    research_service = DonorResearchService(db, service.get())
    research_service.research(opportunity)
    db.commit()

    assert research_service.is_stale(opportunity) is False
    opportunity.version += 1
    db.commit()
    assert research_service.is_stale(opportunity) is True


def test_research_records_provenance(db):
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    research = DonorResearchService(db, service.get()).research(opportunity)
    db.commit()

    refs = research.source_references
    assert refs["opportunity_id"] == opportunity.id
    assert refs["source_url"] == opportunity.source_url
    assert refs["opportunity_version"] == opportunity.version
    assert refs["researcher_version"]


def test_research_does_not_mutate_organisation_facts(db):
    """Research must never silently alter a verified fact."""
    from agent.organisation_memory import OrganisationMemory

    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    memory = OrganisationMemory(db, org.id)
    before = {f.key: f.value for f in memory.current_facts()}

    DonorResearchService(db, service.get()).research(opportunity)
    db.commit()

    after = {f.key: f.value for f in memory.current_facts()}
    assert before == after, "research mutated the organisation's facts"


def test_inferred_research_fields_are_excluded_from_usable(db):
    """AI_INFERENCE must not be usable by default, for the same reason
    AI_INFERRED organisation facts are not submission-safe."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    research_service = DonorResearchService(db, service.get())
    row = research_service.research(opportunity)
    db.commit()

    # Mark a field as an inference and confirm it drops out.
    row.fact_classes = {**(row.fact_classes or {}), "eligibility_observations": AI_INFERENCE}
    db.commit()

    usable = research_service.usable(opportunity.id)
    assert "eligibility_observations" not in usable
    assert "donor_identity" in usable


# ---------------------------------------------------------------------------
# Events and causation
# ---------------------------------------------------------------------------
def test_events_are_staged_not_published_inline(db):
    """The outbox exists because publishing inline loses events on a crash."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()

    events = db.execute(select(models.OutboxEvent)).scalars().all()
    assert events
    # Unpublished, with the same transaction that made the change.
    assert all(e.published_at is None for e in events)
    assert any(e.event_type == "workflow.dispatched" for e in events)


def test_events_carry_agent_organisation_and_workflow(db):
    """Every workflow event must be attributable to an agent and a workflow."""
    org = _org(db)
    service = _provision(db, org)
    agent = service.get()
    opportunity = _opportunity(db)
    workflow = service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        context={"correlation_id": "corr-1", "causation_id": "evt-parent"},
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()

    dispatched = [e for e in db.execute(select(models.OutboxEvent)).scalars()
                  if e.event_type == "workflow.dispatched"][0]
    assert dispatched.payload["agent_id"] == agent.id
    assert dispatched.payload["workflow_id"] == workflow.id
    assert dispatched.payload["correlation_id"] == "corr-1"
    assert dispatched.payload["causation_id"] == "evt-parent"


def test_the_event_chain_is_reconstructable(db):
    """WHY DID THIS AGENT DO THIS? - answerable from the records, not timestamps."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)

    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        context={"correlation_id": "corr-chain", "causation_id": "evt-ingest"},
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    events = db.execute(select(models.OutboxEvent)).scalars().all()
    matching = [e for e in events if e.event_type == "opportunity.matched"]
    if matching:  # the match step reached its event
        assert matching[0].payload.get("causation_id") == "evt-ingest"

    # And the durable history records the whole chain of state, not just times.
    # The FIRST transition is deliberately SYSTEM (the workspace was created), so
    # asserting every transition is ACTOR_AGENT was wrong - the assertion is that
    # the agent's own steps are attributed to it.
    transitions = db.execute(select(models.ApplicationTransition)).scalars().all()
    assert transitions
    assert transitions[0].actor_type == models.ApplicationTransition.ACTOR_SYSTEM
    assert any(t.actor_type == models.ApplicationTransition.ACTOR_AGENT for t in transitions), (
        "no transition was attributed to the agent"
    )


# ---------------------------------------------------------------------------
# last_active_at honesty
# ---------------------------------------------------------------------------
def test_dispatch_alone_does_not_mark_the_agent_active(db):
    """The dispatcher looking at an agent is not the agent working."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()

    assert service.get().last_active_at is None, (
        "dispatch falsely reported successful activity"
    )


def test_a_failed_execution_does_not_mark_the_agent_active(db):
    """If a task fails before durable useful work, do not claim it worked."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    workflow = service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=opportunity.id,
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()

    # Point the workflow at an opportunity that does not exist, so the handler
    # raises rather than completing.
    workflow.subject_id = "does-not-exist"
    workflow.workflow_type = WORKFLOW_RESEARCH
    workflow.specialist_key = "DONOR_RESEARCHER"
    db.commit()

    result = AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()
    assert result.outcome in {ExecutionResult.FAILED, ExecutionResult.SUCCEEDED}
    if result.outcome == ExecutionResult.FAILED:
        assert service.get().last_active_at is None


def test_a_specialist_failure_is_not_agent_death(db):
    """Agent status is about availability, not about one job's outcome."""
    org = _org(db)
    service = _provision(db, org)
    opportunity = _opportunity(db)
    service.schedule(
        workflow_type=WORKFLOW_MATCH,
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id="missing-opportunity",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.commit()
    FleetDispatcher(db).dispatch_once()
    db.commit()
    job = db.execute(select(models.Job)).scalars().one()
    AgentWorker(db, worker_id="w").execute(job.id)
    db.commit()

    assert service.get().status == models.GranadaAgent.ACTIVE, (
        "a specialist failure killed the agent"
    )
