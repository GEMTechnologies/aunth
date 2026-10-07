"""The whole journey, end to end, on one database.

WHY THIS EXISTS
---------------
After eleven phases there were 48 test modules and **not one of them walked the headline
flow**. Every stage was tested in isolation: ingestion, matching, readiness, submission,
award handover, notification routing. Nothing proved the stages are *connected* - and the
integration defects this project has actually shipped were exactly of that kind:

* an undefined `_agent_for` helper in the delivery routes, which `py_compile` accepted
  because Python resolves global names at call time;
* `deliver_event(self.session, ...)` where the attribute is `self.db`, in the relay's drain
  loop, where only an integration test would reach it.

Both passed their unit tests. Neither was reachable except by running the pipeline.

WHAT IT WALKS
-------------
The brief's headline flow, in order:

    Profile -> Discover -> Ingest -> Match -> Qualify -> Prepare
            -> Human approval -> Submit -> Award -> Delivery -> Notify

Each stage asserts the *gate* that makes the next one legitimate, not merely that it
returns something. A journey test that only checks "no exception was raised" would pass on
a system that quietly filed an application without authorisation.

And it ends with the guarantees that must hold on EVERY path:

    production emails sent   = 0
    applications filed       = 0
    external actions taken   = 0
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
from agent.delivery.service import DeliveryService  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.matching import EligibilityEngine  # noqa: E402
from agent.notifications.integration import deliver_event  # noqa: E402
from agent.notifications.providers.fake import InAppChannel  # noqa: E402
from agent.notifications.service import NotificationService  # noqa: E402
from agent.opportunity_ingestion import (  # noqa: E402
    OpportunityIngestionAdapter,
    RawOpportunity,
)
from agent.organisation_memory import (  # noqa: E402
    DocumentVault,
    OrganisationMemory,
    checksum_bytes,
)
from agent.submission.providers.fake import FakeHandoffBuilder, FakeSubmissionProvider  # noqa: E402
from agent.submission.service import SubmissionService  # noqa: E402
from agent.workspace import ApplicationWorkspace  # noqa: E402
from events.relay import OutboxRelay  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RecordingPublisher:
    """Stands in for Redis. Accepts everything and remembers it."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish_raw(self, *, stream: str, fields: dict) -> None:
        self.published.append({"stream": stream, "fields": fields})


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "journey.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def organisation(db):
    """An organisation with a provisioned agent and a verified profile."""
    user = models.User(id=str(uuid.uuid4()), display_name="Grace")
    db.add(user)
    db.commit()

    org = models.Organisation(
        id=str(uuid.uuid4()), name="War Child Test",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()

    service = GranadaAgentService(db, org.id)
    service.provision(autonomy="MONITOR_ONLY")
    db.commit()
    return org, user, service.get()


def _record_profile(db, org):
    """PROFILE: verified facts and an approved document.

    `VERIFIED` rather than `INFERRED`, because the submission path refuses to file an
    application built on inferred facts - the brief's "never fabricate organisational
    facts" rule, and the reason this stage has a state rather than a boolean.
    """
    memory = OrganisationMemory(db, org.id)
    for key, value in (
        ("country", "Uganda"),
        ("organisation_type", "NGO"),
        ("organisation_name", "War Child Test"),
        ("registration_valid_until", "2035-01-01"),
    ):
        memory.record_fact(
            key=key, value=value, state=models.OrgFact.VERIFIED, source="user:owner",
            valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc),
        )
    db.commit()

    vault = DocumentVault(db, org.id)
    document = vault.add_version(
        title="Audited Financial Statements 2026",
        doc_type="audited_financial_statements",
        storage_key=f"org/{org.slug}/afs.pdf",
        checksum_sha256=checksum_bytes(b"audited accounts 2026"),
        mime_type="application/pdf",
        valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc),
    )
    vault.approve(document, approved_by=org.owner_user_id)
    db.commit()
    return document


def _ingest(db, *, deadline: datetime, criteria: str) -> models.Opportunity:
    """DISCOVER + INGEST: a raw opportunity through the real adapter."""
    adapter = OpportunityIngestionAdapter(db)
    raw = RawOpportunity(
        title="Child Protection Grant 2027",
        source_url=f"https://unicef.org/grants/{uuid.uuid4().hex[:10]}",
        source_name="UNICEF",
        country="Uganda",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        description="Funding for child protection work in Uganda.",
        deadline=deadline,
        amount_min=50_000,
        amount_max=150_000,
        currency="USD",
        sector="Child protection",
        eligibility_criteria=criteria,
        application_process="Apply online.",
        contact_email="grants@unicef.org",
        is_active=True,
        scraped_at=_now(),
    )
    outcome = adapter.ingest(raw)
    db.commit()
    assert outcome.saved is True, "the opportunity was not stored, so nothing can follow"
    opportunity = db.query(models.Opportunity).filter(
        models.Opportunity.source_url == raw.source_url
    ).one()
    return opportunity


# ===========================================================================
# THE JOURNEY
# ===========================================================================
def test_the_whole_journey_from_an_empty_database_to_a_notified_person(db, organisation):
    """One organisation, one opportunity, through every gate, to a person being told.

    Ordered exactly as the brief describes the flow, so a failure names the stage that
    broke rather than the function that raised.
    """
    org, user, agent = organisation

    # -- PROFILE -------------------------------------------------------
    document = _record_profile(db, org)
    # `approval_status`, not `status` - and the attribute is read on the left of the `or`,
    # so a wrong name raises rather than falling through to the fallback.
    assert document.approval_status == models.Document.APPROVED
    assert document.approved_by == org.owner_user_id

    # -- DISCOVER + INGEST --------------------------------------------
    opportunity = _ingest(
        db,
        deadline=_now() + timedelta(days=60),
        criteria="Registered NGOs in Uganda with audited accounts.",
    )
    assert opportunity.is_active is True

    # -- MATCH + QUALIFY ----------------------------------------------
    qualification = EligibilityEngine(OrganisationMemory(db, org.id)).qualify(opportunity)
    assert qualification.passed, (
        f"a fully-profiled Ugandan NGO failed qualification: {qualification.summary()}"
    )

    # -- PREPARE -------------------------------------------------------
    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()
    readiness = workspace.readiness(application)
    assert readiness.ready, f"the application is not ready to submit: {readiness.blockers}"

    # -- FREEZE + HUMAN APPROVAL --------------------------------------
    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    answers = [
        {"question": "Legal name", "answer": "War Child Test",
         "source": "org_fact:organisation_name", "verified": True},
        {"question": "Safeguarding approach",
         "answer": "We follow the Uganda national safeguarding framework.",
         "source": "model:draft", "verified": False},
    ]
    budget = {"currency": "USD", "total": 150_000,
              "lines": [{"item": "staff", "amount": 100_000},
                        {"item": "materials", "amount": 50_000}]}
    package = submission.build_package(
        application=application, documents=[document], answers=answers, budget=budget,
        contact_email="grants@warchild.org",
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_HANDOFF,
    )
    db.commit()
    assert package.status == models.SubmissionPackage.AWAITING_AUTHORISATION

    # THE GATE: nothing may be filed or handed off before this, and the refusal is the
    # product's central guarantee rather than a formality.
    blocked = submission.handoff(package_id=package.id)
    assert blocked.refused is True
    assert blocked.refusal_code == "NOT_AUTHORISED"

    package = submission.authorise(package_id=package.id, user_id=org.owner_user_id)
    db.commit()
    assert package.status == models.SubmissionPackage.AUTHORISED
    assert package.package_fingerprint

    # -- SUBMIT (HANDOFF: NO EXTERNAL ACTION) -------------------------
    provider = FakeSubmissionProvider()
    submission.provider = provider
    submission.handoff_builder = FakeHandoffBuilder()
    run = submission.handoff(package_id=package.id)
    db.commit()

    assert run.handoff is not None and run.handoff.steps
    assert package.status != models.SubmissionPackage.SUBMITTED, (
        "a handoff marked the application submitted without a receipt"
    )
    assert provider.submission_count == 0, "the handoff filed something"

    # A person files it and records the funder's reference.
    submission.record_receipt(
        package_id=package.id, reference="UNICEF-2027-0042",
        captured_by=org.owner_user_id,
        acknowledgement_text="Application received.",
    )
    db.commit()
    assert package.status == models.SubmissionPackage.SUBMITTED

    # -- AWARD ---------------------------------------------------------
    delivery = DeliveryService(db, org_id=org.id, agent_id=agent.id)
    handover = delivery.handover(
        package=package, reference="GRANT-2027-001", awarded_amount=150_000,
        starts_on=_now(), ends_on=_now() + timedelta(days=365),
        donor_name="UNICEF", title="Child Protection Grant 2027",
        conditions=[
            {"title": "Signed grant agreement",
             "kind": models.GrantCondition.KIND_PRECONDITION,
             "due_on": _now() + timedelta(days=14)},
        ],
        reporting_schedule=[
            {"title": "Q1 narrative report", "due_on": _now() + timedelta(days=90),
             "kind": models.ReportingObligation.KIND_NARRATIVE,
             "period": models.ReportingObligation.PERIOD_QUARTERLY},
        ],
        disbursement_schedule=[
            {"amount": 75_000, "label": "First tranche", "tranche_number": 1,
             "expected_on": _now() + timedelta(days=30)},
        ],
    )
    db.commit()

    # THE EXIT CRITERION: nothing a person had to key in that the application already had.
    assert handover.re_entered_fields == [], (
        f"the handover required manual re-entry of {handover.re_entered_fields}"
    )
    grant = db.query(models.Grant).filter(models.Grant.id == handover.grant_id).one()
    assert grant.requested_amount == 150_000
    assert grant.awarded_amount == 150_000
    assert grant.source_package_id == package.id, "the grant has lost its provenance"
    assert grant.approved_budget["total"] == 150_000

    # -- DELIVERY: A REPORT GOES OVERDUE ---------------------------------
    obligation = db.query(models.ReportingObligation).filter(
        models.ReportingObligation.grant_id == grant.id
    ).one()
    obligation.due_on = _now() - timedelta(days=2)
    db.commit()

    moved = delivery.refresh_reporting_statuses()
    db.commit()
    assert moved["overdue"] == 1
    assert obligation.status == models.ReportingObligation.STATUS_OVERDUE

    # -- NOTIFY --------------------------------------------------------
    # The relay drains the outbox and routes what matters to a person. This is the step
    # that did not exist until Phase 10: the event was published and read by nobody.
    event = db.query(models.OutboxEvent).filter(
        models.OutboxEvent.event_type == "granada:v1:report.overdue"
    ).first()
    assert event is not None, "no overdue event was emitted, so nobody could be told"

    channel = InAppChannel()
    outcome = deliver_event(db, event=event, channels=[channel])
    db.commit()

    assert outcome is not None, "the overdue report was unrouted"
    assert channel.delivery_count >= 1, "the event reached a stream and not a person"

    notification = db.query(models.Notification).one()
    assert notification.severity == models.Notification.SEVERITY_CRITICAL
    assert notification.action_required is True
    assert "Q1 narrative report" in notification.title

    # Suppression holds on the real path, not only in the unit test: the same condition
    # scanned again is a repeat, not a second notification.
    deliver_event(db, event=event, channels=[channel])
    db.commit()
    assert db.query(models.Notification).count() == 1

    # -- WHAT A PERSON SEES --------------------------------------------
    inbox_service = NotificationService(
        db, org_id=org.id, channels=[channel], default_recipients=[org.owner_user_id]
    )
    summary = inbox_service.summary(user_id=org.owner_user_id)
    assert summary["critical"] == 1
    assert summary["action_required"] == 1

    compliance = delivery.compliance_summary()
    assert compliance["counts"]["overdue_reports"] == 1
    assert compliance["counts"]["blocking_conditions"] == 1, (
        "the unsigned agreement blocks a payment and the summary does not say so"
    )
    assert compliance["portfolio"]["scheduled_total"] == "75000.00"


def test_the_journey_ends_with_every_guarantee_intact(db, organisation):
    """THE assertion this whole file exists to make.

    Run the flow, then check that nothing irreversible happened. A journey test that only
    proved the stages connect would pass on a system that quietly sent an email and filed
    an application along the way.
    """
    org, user, agent = organisation
    document = _record_profile(db, org)
    opportunity = _ingest(
        db, deadline=_now() + timedelta(days=60),
        criteria="Registered NGOs in Uganda with audited accounts.",
    )

    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()

    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    package = submission.build_package(
        application=application, documents=[document],
        answers=[{"question": "Legal name", "answer": "War Child Test",
                  "source": "org_fact:organisation_name", "verified": True}],
        budget={"currency": "USD", "total": 1000, "lines": [{"item": "work", "amount": 1000}]},
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_HANDOFF,
    )
    db.commit()
    submission.authorise(package_id=package.id, user_id=org.owner_user_id)
    db.commit()

    # An ADAPTER submission is attempted with no provider. It must be refused, not filed.
    submission.provider = None
    refused = submission.execute(package_id=package.id)
    assert refused.refused is True
    assert refused.refusal_code == "NO_SUBMISSION_PROVIDER"

    # And a handoff, which is the path that IS implemented, completes without submitting.
    submission.handoff_builder = FakeHandoffBuilder()
    submission.handoff(package_id=package.id)
    db.commit()

    # -- THE GUARANTEES ------------------------------------------------
    assert db.query(models.SubmissionAttempt).filter(
        models.SubmissionAttempt.result == models.SubmissionAttempt.CONFIRMED_SUBMITTED
    ).count() == 0, "AN APPLICATION WAS FILED"

    assert db.query(models.MailSendAttempt).filter(
        models.MailSendAttempt.result == models.MailSendAttempt.CONFIRMED_SENT
    ).count() == 0, "AN EMAIL WAS SENT"

    assert db.query(models.MailSendIntent).count() == 0
    assert db.query(models.SubmissionReceipt).count() == 0, (
        "a receipt exists without a person recording one"
    )


def test_the_journey_refuses_to_skip_the_approval_gate(db, organisation):
    """A journey test that only walks the happy path cannot show that the gates gate.

    This walks the flow to the point of filing and then asserts that every route past the
    gate is closed.
    """
    org, user, agent = organisation
    document = _record_profile(db, org)
    opportunity = _ingest(
        db, deadline=_now() + timedelta(days=60),
        criteria="Registered NGOs in Uganda with audited accounts.",
    )
    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()

    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    package = submission.build_package(
        application=application, documents=[document], answers=[],
        budget={"currency": "USD", "total": 1000, "lines": [{"item": "work", "amount": 1000}]},
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_ADAPTER,
    )
    db.commit()

    provider = FakeSubmissionProvider()
    submission.provider = provider

    # 1. Filing without an authorisation.
    assert submission.execute(package_id=package.id).refusal_code == "NOT_AUTHORISED"

    # 2. A handoff without an authorisation.
    assert submission.handoff(package_id=package.id).refusal_code == "NOT_AUTHORISED"

    # 3. A receipt without the funder's reference.
    from agent.submission.service import SubmissionError

    with pytest.raises(SubmissionError):
        submission.record_receipt(package_id=package.id, reference="")

    # 4. Nothing reached the provider at any point.
    assert provider.call_count == 0
    assert provider.submission_count == 0
    assert db.query(models.SubmissionAttempt).count() == 0

    # 5. And authorising the WRONG package does not authorise this one: changing the
    #    artefact set produces a new fingerprint, and the authorisation does not travel.
    submission.authorise(package_id=package.id, user_id=org.owner_user_id)
    db.commit()

    changed = submission.build_package(
        application=application, documents=[document], answers=[],
        budget={"currency": "USD", "total": 999_999, "lines": [{"item": "work", "amount": 999_999}]},
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_ADAPTER,
    )
    db.commit()
    assert changed.id != package.id, "a changed budget re-used the authorised package"
    assert changed.status == models.SubmissionPackage.AWAITING_AUTHORISATION

    # The new package cannot be filed on the old authorisation.
    fresh = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    fresh.provider = provider
    assert fresh.execute(package_id=changed.id).refusal_code == "NOT_AUTHORISED"
    assert provider.submission_count == 0


def test_the_journey_is_scoped_to_one_organisation(db, organisation):
    """Tenancy is not a property of a request, it is a property of the data.

    A second organisation created in the same database must be unable to see or act on the
    first one's grant, package or notification.
    """
    org, user, agent = organisation
    document = _record_profile(db, org)
    opportunity = _ingest(
        db, deadline=_now() + timedelta(days=60),
        criteria="Registered NGOs in Uganda with audited accounts.",
    )
    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()

    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    package = submission.build_package(
        application=application, documents=[document], answers=[],
        budget={"currency": "USD", "total": 1000, "lines": [{"item": "work", "amount": 1000}]},
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_HANDOFF,
    )
    db.commit()

    # A second organisation.
    other_user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(other_user)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other NGO",
        slug=f"other-{uuid.uuid4().hex[:8]}", owner_user_id=other_user.id,
    )
    db.add(other)
    db.commit()
    other_agent = GranadaAgentService(db, other.id)
    other_agent.provision(autonomy="MONITOR_ONLY")
    db.commit()

    stranger = SubmissionService(db, org_id=other.id, agent_id=other_agent.get().id)
    # Every route to the first organisation's package is closed.
    assert stranger.handoff(package_id=package.id).refusal_code == "NOT_FOUND"
    assert stranger.execute(package_id=package.id).refusal_code == "NOT_FOUND"

    stranger_delivery = DeliveryService(db, org_id=other.id, agent_id=other_agent.get().id)
    assert stranger_delivery.compliance_summary()["grants_active"] == 0
    assert stranger_delivery.deadlines(within_days=365) == []


def test_the_relay_drains_the_whole_journey_without_losing_an_event(db, organisation):
    """The outbox is the seam between the transaction and everything downstream.

    An event that is committed but never published is invisible: the work succeeded and
    nothing else knows. This walks the pipeline and then drains, asserting that every event
    the journey produced reached the stream.
    """
    org, user, agent = organisation
    document = _record_profile(db, org)
    opportunity = _ingest(
        db, deadline=_now() + timedelta(days=60),
        criteria="Registered NGOs in Uganda with audited accounts.",
    )
    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()

    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    package = submission.build_package(
        application=application, documents=[document],
        answers=[{"question": "Legal name", "answer": "War Child Test",
                  "source": "org_fact:organisation_name", "verified": True}],
        budget={"currency": "USD", "total": 150_000,
                "lines": [{"item": "staff", "amount": 150_000}]},
        target_url="https://unicef.org/apply",
        mode=models.SubmissionPackage.MODE_HANDOFF,
    )
    db.commit()
    submission.authorise(package_id=package.id, user_id=org.owner_user_id)
    db.commit()
    package = submission.record_receipt(
        package_id=package.id, reference="UNICEF-1", captured_by=org.owner_user_id
    )
    db.commit()

    delivery = DeliveryService(db, org_id=org.id, agent_id=agent.id)
    delivery.handover(
        package=package, reference="GRANT-1", awarded_amount=150_000,
        reporting_schedule=[
            {"title": "Q1 report", "due_on": _now() - timedelta(days=1),
             "kind": models.ReportingObligation.KIND_NARRATIVE},
        ],
    )
    db.commit()
    delivery.refresh_reporting_statuses()
    db.commit()

    pending_before = db.query(models.OutboxEvent).filter(
        models.OutboxEvent.published_at.is_(None)
    ).count()
    assert pending_before > 0, "the journey produced no events at all"

    relay = OutboxRelay(db, RecordingPublisher())
    published = relay.drain_once()
    db.commit()

    assert published == pending_before, (
        f"{pending_before - published} event(s) were never published - the work succeeded "
        "and nothing downstream will ever know"
    )
    assert db.query(models.OutboxEvent).filter(
        models.OutboxEvent.published_at.is_(None)
    ).count() == 0
    assert relay.notified >= 1, "the overdue report reached the stream and not a person"
