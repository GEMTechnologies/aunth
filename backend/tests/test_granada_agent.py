"""The persistent Granada Agent: one per organisation, logical not a process.

The load-bearing tests are:

* ``test_a_specialist_cannot_exceed_its_agent_authority`` — the parent is the
  ceiling, so an agent cannot be escalated by the component it delegated to.
* ``test_a_worker_takes_the_agent_from_the_job_row_not_the_message`` — the same
  rule as the tenant, for the same reason.
* ``test_one_agent_per_organisation`` — the product promise, as a constraint
  rather than a convention.
* ``test_there_is_no_per_agent_scheduler`` — the architectural correction stated
  as an assertion, so a future edit that adds a process per customer fails here.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import (  # noqa: E402
    SPECIALIST_KEYS,
    SPECIALISTS,
    AgentError,
    AgentNotFound,
    AgentPaused,
    AuthorityExceeded,
    GranadaAgentService,
)


@pytest.fixture
def db(tmp_path):
    # Schema copied from a session template rather than rebuilt: create_all to a
    # file on this filesystem costs ~3.8s per test because the schema has 38 tables
    # and 203 indexes. See tests/conftest.py::make_sqlite_db.
    engine, session = make_sqlite_db(tmp_path, "agent.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="War Child", slug=None):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    slug = slug or f"org-{uuid.uuid4().hex[:8]}"
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name, slug=slug, owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def org(db):
    return _org(db)


@pytest.fixture
def service(db, org):
    return GranadaAgentService(db, org.id)


# ---------------------------------------------------------------------------
# Provisioning: one agent per organisation
# ---------------------------------------------------------------------------
def test_one_agent_per_organisation(service, db, org):
    """The product promise: create your profile once, get one agent."""
    first = service.provision()
    second = service.provision()
    db.commit()

    assert first.id == second.id
    assert len(db.execute(select(models.GranadaAgent)).scalars().all()) == 1
    assert first.org_id == org.id


def test_the_database_enforces_one_agent_per_organisation(service, db, org):
    """A constraint, not a convention.

    Two agents for one organisation would mean two sets of autonomy settings and
    two answers to "who is acting for War Child".
    """
    service.provision()
    db.commit()
    db.add(models.GranadaAgent(
        org_id=org.id, display_name="Second", vertical="NGO",
        status="ACTIVE", autonomy="MONITOR_ONLY", version=1,
        created_at=datetime.now(timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_provisioning_returns_an_existing_agent_rather_than_racing(service, db, org):
    """Called from registration, which can be retried."""
    a = service.provision()
    b = service.provision(display_name="Something Else")
    assert a.id == b.id


def test_the_agent_is_named_after_the_organisation(service, db, org):
    """The customer's first sight of their agent should read like theirs."""
    agent = service.provision()
    assert agent.display_name == f"{org.name} Agent"


def test_provisioning_creates_the_full_specialist_roster(service, db, org):
    """An agent is not one opaque worker."""
    service.provision()
    db.commit()
    assert [s.key for s in service.specialists()] == list(SPECIALIST_KEYS)
    assert len(SPECIALIST_KEYS) == 10


def test_specialists_display_in_pipeline_order(service, db, org):
    """The display should match the customer's mental model, not a dict's order."""
    service.provision()
    db.commit()
    names = [s.display_name for s in service.specialists()]
    assert names[0] == "Funding Hunter"
    assert "Proposal Agent" in names
    assert names.index("Proposal Agent") < names.index("Submission Agent")


def test_provisioning_requires_a_real_organisation(service, db):
    """An agent for a non-existent organisation is a dangling reference."""
    with pytest.raises(AgentNotFound):
        GranadaAgentService(db, str(uuid.uuid4())).provision()


def test_provisioning_rejects_an_unknown_vertical(service):
    with pytest.raises(AgentError):
        service.provision(vertical="MINING")


def test_provisioning_rejects_an_unknown_autonomy_level(service):
    with pytest.raises(AgentError):
        service.provision(autonomy="DO_WHATEVER")


def test_the_service_refuses_an_unknown_tenant(db):
    with pytest.raises(AgentError) as excinfo:
        GranadaAgentService(db, "")
    assert "deny" in str(excinfo.value)


def test_an_unprovisioned_organisation_reports_rather_than_inventing_one(db):
    other = _org(db, name="No Agent Yet")
    with pytest.raises(AgentNotFound):
        GranadaAgentService(db, other.id).require()


# ---------------------------------------------------------------------------
# Authority: the parent is the ceiling
# ---------------------------------------------------------------------------
def test_a_specialist_cannot_exceed_its_agent_authority(service, db, org):
    """An agent must not be escalated by the component it delegated to.

    This is the check that keeps a specialist from granting itself the authority
    its owner never gave it.
    """
    service.provision(autonomy=Autonomy.DRAFT_ONLY)
    db.commit()

    # Within the ceiling: fine.
    service.require_authority("PROPOSAL_WRITER", Autonomy.MONITOR_ONLY)

    # Beyond it: refused, and the message says which level was needed and held.
    with pytest.raises(AuthorityExceeded) as excinfo:
        service.require_authority("SUBMISSION", Autonomy.AUTOPILOT_WITH_GATES)
    message = str(excinfo.value)
    assert "DRAFT_ONLY" in message
    assert "cannot exceed" in message


def test_every_specialist_is_bounded_by_the_agent(service, db, org):
    """The property holds for the whole roster, not for one example."""
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()
    for key in SPECIALIST_KEYS:
        service.require_authority(key, Autonomy.MONITOR_ONLY)
        for higher in (Autonomy.DRAFT_ONLY, Autonomy.AUTO_ROUTINE, Autonomy.AUTOPILOT_WITH_GATES):
            with pytest.raises(AuthorityExceeded):
                service.require_authority(key, higher)


def test_a_paused_agent_must_not_act(service, db, org):
    """Pausing is the customer's stop button and it must actually stop things."""
    agent = service.provision()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()

    with pytest.raises(AgentPaused):
        service.require_authority("PROPOSAL_WRITER", Autonomy.MONITOR_ONLY)


def test_an_unknown_specialist_is_refused(service, db, org):
    service.provision()
    db.commit()
    with pytest.raises(AgentError):
        service.require_authority("CHIEF_EXECUTIVE", Autonomy.MONITOR_ONLY)


def test_changing_autonomy_bumps_the_version(service, db, org):
    """A cached decision made under the old level must be able to tell.

    Without the bump, work authorised under a level that has since been withdrawn
    would complete on authority the customer no longer grants.
    """
    agent = service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()
    before = agent.version

    service.set_autonomy(Autonomy.AUTO_ROUTINE)
    db.commit()
    assert agent.version == before + 1
    assert agent.autonomy == Autonomy.AUTO_ROUTINE


def test_changing_autonomy_rejects_an_unknown_level(service, db, org):
    service.provision()
    db.commit()
    with pytest.raises(AgentError):
        service.set_autonomy("FULL_SEND")


# ---------------------------------------------------------------------------
# The worker takes the agent from the durable record
# ---------------------------------------------------------------------------
def test_a_worker_takes_the_agent_from_the_job_row_not_the_message(service, db, org):
    """A queued payload is transport, and transport is not authoritative.

    The tenant already comes from the ledger rather than the message. The agent
    does too, because a forged or corrupted payload must not be able to make a
    worker act as a different organisation's agent.

    The payload here names **another organisation's real agent**, not a random
    UUID. The first version of this test used a UUID that matched nothing, so the
    lookup fell through to the tenant fallback and the test passed even with the
    payload being trusted - it could not fail under the mutation it targeted.
    """
    service.provision()
    db.commit()
    mine = service.get()

    other = _org(db, name="Rival NGO", slug="rival-ngo")
    other_service = GranadaAgentService(db, other.id)
    other_service.provision()
    db.commit()
    theirs = other_service.get()
    assert theirs.id != mine.id

    job = models.Job(
        org_id=org.id, agent_id=mine.id, stream="granada:v1:jobs:x",
        job_type="opportunity_triage", state=models.Job.QUEUED,
        payload={"agent_id": theirs.id},  # a claim to be someone else's agent
        available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    resolved = GranadaAgentService.for_job(db, job)
    assert resolved is not None
    assert resolved.org_id == org.id, "the payload's agent_id was trusted"
    assert resolved.get().id == mine.id
    assert resolved.org_id != other.id


def test_a_job_with_no_agent_falls_back_to_its_tenant(db, org):
    """System work belongs to no agent, and the tenant is still the scope."""
    job = models.Job(
        org_id=org.id, stream="granada:v1:jobs:x", job_type="system",
        state=models.Job.QUEUED, available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    resolved = GranadaAgentService.for_job(db, job)
    assert resolved is not None and resolved.org_id == org.id


def test_a_job_with_neither_agent_nor_tenant_resolves_to_nothing(db):
    """Unattributed work must not be attributed to a customer by default."""
    job = models.Job(
        org_id=None, stream="granada:v1:jobs:x", job_type="system",
        state=models.Job.QUEUED, available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    assert GranadaAgentService.for_job(db, job) is None


def test_the_jobs_table_carries_an_agent_column(db):
    """The column that makes one shared worker pool possible."""
    columns = {c["name"] for c in inspect(db.get_bind()).get_columns("jobs")}
    assert "agent_id" in columns


# ---------------------------------------------------------------------------
# Logical autonomy, not a process per customer
# ---------------------------------------------------------------------------
def test_there_is_no_per_agent_scheduler(db, service, org):
    """The architectural correction, as an assertion.

    Ten thousand NGOs must not become ten thousand running processes. What
    replaces that is `jobs.agent_id` plus a shared dispatcher. If someone later
    adds a per-agent scheduler table, queue or heartbeat, this fails and they have
    to argue for it deliberately rather than adding it by accident.
    """
    service.provision()
    db.commit()
    tables = set(inspect(db.get_bind()).get_table_names())
    per_agent_machinery = {
        "agent_schedulers", "agent_processes", "agent_heartbeats",
        "agent_locks", "agent_leases", "agent_workers",
    }
    assert tables & per_agent_machinery == set(), (
        "per-agent runtime machinery appeared; the design is one shared worker "
        "pool with jobs.agent_id, not a process per customer"
    )
    # And the shared mechanism is present.
    assert "jobs" in tables and "agent_workflows" in tables


def test_the_dispatcher_query_is_fleet_wide_not_per_agent(db, service, org):
    """One sweep of the whole fleet, not one sweeper per customer."""
    service.provision()
    db.commit()
    service.schedule(workflow_type="hunt", run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.commit()

    due = service.due_workflows()
    assert len(due) == 1
    # The same call returns work for a *different* agent, proving it is not
    # scoped to this one.
    other = _org(db, name="Other NGO", slug="other-ngo")
    other_service = GranadaAgentService(db, other.id)
    other_service.provision()
    other_service.schedule(
        workflow_type="hunt", run_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.commit()
    assert len(service.due_workflows()) == 2


def test_a_workflow_carries_its_agent(db, service, org):
    """Every autonomous workflow belongs to a persistent agent.

    A workflow that is anonymous work is background-job software; a workflow that
    is *War Child's agent pursuing this opportunity* is the product.
    """
    service.provision()
    db.commit()
    workflow = service.schedule(
        workflow_type="opportunity_pursuit",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=str(uuid.uuid4()),
        specialist_key="MATCHER",
    )
    db.commit()
    assert workflow.agent_id == service.get().id
    assert workflow.org_id == org.id


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------
def test_scheduling_the_same_subject_twice_does_not_duplicate(db, service, org):
    """Two identical workflows would mean the same opportunity pursued twice."""
    service.provision()
    db.commit()
    subject = str(uuid.uuid4())
    first = service.schedule(
        workflow_type="pursuit",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY, subject_id=subject,
    )
    second = service.schedule(
        workflow_type="pursuit",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY, subject_id=subject,
    )
    db.commit()
    assert first.id == second.id
    assert len(db.execute(select(models.AgentWorkflow)).scalars().all()) == 1


def test_the_database_enforces_workflow_uniqueness(db, service, org):
    service.provision()
    db.commit()
    subject = str(uuid.uuid4())
    service.schedule(
        workflow_type="pursuit",
        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY, subject_id=subject,
    )
    db.commit()
    agent = service.get()
    db.add(models.AgentWorkflow(
        agent_id=agent.id, org_id=org.id, workflow_type="pursuit",
        state="PENDING", subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
        subject_id=subject, created_at=datetime.now(timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_waiting_is_scheduled_not_blocked(db, service, org):
    """A workflow parked on a deadline costs a row, not a process."""
    service.provision()
    db.commit()
    workflow = service.schedule(workflow_type="follow_up")
    until = datetime.now(timezone.utc) + timedelta(days=3)
    service.wait(workflow, on="follow-up window opens", until=until)
    db.commit()

    assert workflow.state == models.AgentWorkflow.WAITING
    assert workflow.waiting_on == "follow-up window opens"
    assert workflow.next_run_at is not None
    # Not due yet, so the dispatcher ignores it until the time comes.
    assert service.due_workflows() == []


def test_a_completed_workflow_is_not_reopened_by_rescheduling(db, service, org):
    """Reopening finished work on a repeat scan is how a loop starts."""
    service.provision()
    db.commit()
    workflow = service.schedule(workflow_type="hunt")
    workflow.state = models.AgentWorkflow.COMPLETED
    db.commit()

    again = service.schedule(workflow_type="hunt")
    assert again.id == workflow.id
    assert again.state == models.AgentWorkflow.COMPLETED


def test_the_dispatcher_orders_by_priority_then_time(db, service, org):
    service.provision()
    db.commit()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    service.schedule(workflow_type="low", subject_id="a", priority=200, run_at=past)
    service.schedule(workflow_type="urgent", subject_id="b", priority=1, run_at=past)
    db.commit()
    assert [w.workflow_type for w in service.due_workflows()] == ["urgent", "low"]


def test_scheduling_rejects_an_unknown_specialist(service, db, org):
    service.provision()
    db.commit()
    with pytest.raises(AgentError):
        service.schedule(workflow_type="x", specialist_key="WIZARD")


def test_scheduling_without_an_agent_is_refused(db):
    """No agent, no work. An unprovisioned organisation has nothing to act for."""
    other = _org(db, name="Provision Me Later")
    service = GranadaAgentService(db, other.id)
    with pytest.raises(AgentNotFound):
        service.schedule(workflow_type="hunt")


# ---------------------------------------------------------------------------
# Activity and the customer-visible summary
# ---------------------------------------------------------------------------
def test_touch_records_that_the_agent_worked(service, db, org):
    """"Active 24/7" and "last worked 3 minutes ago" have to be true."""
    service.provision()
    db.commit()
    assert service.get().last_active_at is None

    service.touch(specialist_key="MATCHER", activity="scoring 12 opportunities")
    db.commit()
    assert service.get().last_active_at is not None

    specialist = service.specialist("MATCHER")
    assert specialist.status == models.AgentSpecialist.ACTIVE
    assert specialist.current_activity == "scoring 12 opportunities"
    assert specialist.runs_completed == 1


def test_releasing_a_specialist_clears_its_activity(service, db, org):
    """An activity string left set is a dashboard that lies."""
    service.provision()
    db.commit()
    service.touch(specialist_key="PROPOSAL_WRITER", activity="drafting")
    service.release_specialist("PROPOSAL_WRITER")
    db.commit()

    specialist = service.specialist("PROPOSAL_WRITER")
    assert specialist.status == models.AgentSpecialist.IDLE
    assert specialist.current_activity is None


def test_the_status_panel_reports_the_promised_numbers(service, db, org):
    """Every figure is a real count over a real table, not a placeholder."""
    service.provision()
    db.commit()
    status = service.status()

    assert status.display_name == "War Child Agent"
    assert status.vertical == "NGO"
    assert status.is_active is True
    assert status.opportunities_scanned_today == 0
    assert status.applications_in_progress == 0
    assert status.actions_requiring_you == 0
    assert status.active_workflows == 0

    payload = status.as_dict()
    for field_name in (
        "agent_id", "display_name", "vertical", "status", "autonomy",
        "opportunities_scanned_today", "applications_in_progress",
        "emails_handled_today", "actions_requiring_you", "active_workflows",
    ):
        assert field_name in payload, f"the panel is missing {field_name}"


def test_the_status_panel_counts_real_work(service, db, org):
    """The numbers must move when work happens, or they are decoration."""
    service.provision()
    db.commit()

    opportunity = models.Opportunity(
        title="Community Health Grant",
        source_url=f"https://f.org/{uuid.uuid4().hex[:8]}",
        source_name="F", country="Uganda",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        is_active=True, created_at=datetime.now(timezone.utc),
    )
    db.add(opportunity)
    db.commit()

    db.add(models.OpportunityMatch(
        org_id=org.id, opportunity_id=opportunity.id,
        state=models.OpportunityMatch.MATCHED, hard_gate_passed=True,
        computed_at=datetime.now(timezone.utc),
    ))
    db.add(models.Application(
        org_id=org.id, opportunity_id=opportunity.id,
        state="WAITING_FOR_APPROVAL", version=1,
        created_at=datetime.now(timezone.utc),
    ))
    service.schedule(workflow_type="pursuit", subject_id=str(uuid.uuid4()))
    db.commit()

    status = service.status()
    assert status.opportunities_scanned_today == 1
    assert status.applications_in_progress == 1
    assert status.actions_requiring_you >= 1, "an approval waiting did not show as an action"
    assert status.active_workflows == 1


def test_the_status_panel_is_scoped_to_one_organisation(service, db, org):
    """War Child's panel must not count another NGO's work - in ANY figure.

    The first version of this test only checked ``active_workflows``, so removing
    the ``org_id`` filter from the *scanned* count did not fail it. A test named
    "scoped to one organisation" that checks one field of six is a test that only
    appears to cover the claim.
    """
    service.provision()
    db.commit()

    other = _org(db, name="Other NGO", slug="other-ngo-2")
    other_service = GranadaAgentService(db, other.id)
    other_service.provision()
    other_service.schedule(workflow_type="pursuit", subject_id=str(uuid.uuid4()))
    db.commit()

    # Give the other organisation real work of every kind the panel counts.
    opportunity = models.Opportunity(
        title="Their grant", source_url=f"https://f.org/{uuid.uuid4().hex[:8]}",
        source_name="F", country="Uganda",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        is_active=True, created_at=datetime.now(timezone.utc),
    )
    db.add(opportunity)
    db.commit()
    db.add(models.OpportunityMatch(
        org_id=other.id, opportunity_id=opportunity.id,
        state=models.OpportunityMatch.NEEDS_DATA, hard_gate_passed=False,
        computed_at=datetime.now(timezone.utc),
    ))
    db.add(models.Application(
        org_id=other.id, opportunity_id=opportunity.id,
        state="WAITING_FOR_APPROVAL", version=1,
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()

    mine = service.status()
    theirs = other_service.status()

    # Ours: nothing. Theirs: everything. Every counted field is asserted, so a
    # missing org_id filter on any one of them fails here.
    assert mine.opportunities_scanned_today == 0
    assert mine.applications_in_progress == 0
    assert mine.actions_requiring_you == 0
    assert mine.active_workflows == 0

    assert theirs.opportunities_scanned_today == 1
    assert theirs.applications_in_progress == 1
    assert theirs.actions_requiring_you >= 1
    assert theirs.active_workflows == 1


def test_the_next_wake_is_reported(service, db, org):
    """The customer should be able to see when their agent next runs."""
    service.provision()
    db.commit()
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    service.schedule(workflow_type="hunt", run_at=soon)
    db.commit()
    status = service.status()
    assert status.next_wake_at is not None
    assert status.next_wake_at >= datetime.now(timezone.utc)


def test_a_paused_agent_still_reports_its_state(service, db, org):
    """Pausing must not blind the customer to what their agent holds."""
    agent = service.provision()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()
    status = service.status()
    assert status.status == models.GranadaAgent.PAUSED
    assert status.is_active is False
