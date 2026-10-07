"""Phase 9: award-to-delivery.

Built around the brief's exit criterion — **no manual re-entry of data already approved
in the application** — and around the three refusals that stop money being lost:

* a **condition** satisfied without evidence;
* a **disbursement** recorded as received without a reference;
* a **report** marked submitted without the funder's acknowledgement.

The last is the one that costs money silently: an unsubmitted narrative report produces
no rejection letter, just a tranche that does not arrive.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.delivery.service import (  # noqa: E402
    DeliveryError,
    DeliveryService,
    NotDeliverable,
)
from tests.test_mail import _opportunity_and_application, _org_and_agent  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "delivery.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    org, agent_service = _org_and_agent(db, with_documents=True)
    agent = agent_service.get()
    _opportunity, application = _opportunity_and_application(db, org)
    return org, agent, application


def _submitted_package(db, world, *, budget=None):
    """An authorised, SUBMITTED package — the only thing a grant may follow."""
    org, agent, application = world
    package = models.SubmissionPackage(
        id=str(uuid.uuid4()),
        org_id=org.id,
        agent_id=agent.id,
        application_id=application.id,
        opportunity_id=application.opportunity_id,
        package_fingerprint="a" * 64,
        manifest={
            "documents": [],
            "answers": [],
            "budget": budget if budget is not None else {
                "currency": "UGX", "total": 120_000_000,
                "lines": [
                    {"item": "staff", "amount": 80_000_000},
                    {"item": "materials", "amount": 40_000_000},
                ],
            },
            "contact_email": "grants@warchild.org",
            "target_url": "https://funder.example.org/apply",
        },
        application_version=1,
        status=models.SubmissionPackage.SUBMITTED,
        submission_mode=models.SubmissionPackage.MODE_HANDOFF,
        idempotency_key=f"app:{uuid.uuid4().hex}",
        created_at=_now(),
        authorised_at=_now(),
        submitted_at=_now(),
        funder_reference="FUNDER-1",
    )
    db.add(package)
    db.commit()
    return package


def _service(db, world):
    org, agent, _application = world
    return DeliveryService(db, org_id=org.id, agent_id=agent.id)


def _handover(db, world, *, awarded=120_000_000, budget=None, **kwargs):
    package = _submitted_package(db, world, budget=budget)
    service = _service(db, world)
    result = service.handover(
        package=package,
        reference=kwargs.pop("reference", "GRANT-2027-001"),
        awarded_amount=awarded,
        starts_on=_now(),
        ends_on=_now() + timedelta(days=365),
        **kwargs,
    )
    db.commit()
    return service, result, package


# ===========================================================================
# THE EXIT CRITERION: NOTHING IS RE-ENTERED
# ===========================================================================
def test_the_grant_derives_its_budget_from_the_authorised_package(db, world):
    """The exit criterion. The budget is the one a human authorised in Phase 8, copied
    rather than keyed in — re-keying an approved budget is how the record and the
    application drift apart, and the drift is found at audit."""
    _service_obj, result, package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()

    assert grant.approved_budget["total"] == 120_000_000
    assert grant.requested_amount == Decimal("120000000")
    assert grant.source_package_id == package.id
    assert result.re_entered_fields == [], (
        f"the handover required manual re-entry of: {result.re_entered_fields}"
    )


def test_the_currency_comes_from_the_application(db, world):
    _service_obj, result, _package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()
    assert grant.currency == "UGX"


def test_the_workplan_is_generated_from_the_budget_lines(db, world):
    """The workplan and the budget are the same data seen two ways, so they cannot
    drift."""
    _service_obj, result, _package = _handover(db, world)
    project = db.query(models.Project).filter(models.Project.id == result.project_id).one()

    milestones = project.baseline_workplan["milestones"]
    assert [m["name"] for m in milestones] == ["staff", "materials"]
    assert [m["amount"] for m in milestones] == ["80000000", "40000000"]
    assert project.baseline_workplan["source"] == "submission_package.approved_budget"


def test_workplan_dates_are_left_unset_rather_than_invented(db, world):
    """A workplan date nobody agreed to is worse than an empty field, because it looks
    decided."""
    _service_obj, result, _package = _handover(db, world)
    project = db.query(models.Project).filter(models.Project.id == result.project_id).one()
    assert all(m["due_on"] is None for m in project.baseline_workplan["milestones"])


def test_a_package_with_no_budget_says_so_rather_than_pretending(db, world):
    """The exit criterion cannot be met if the package carried no budget, and reporting
    that is better than a silently empty workplan."""
    _service_obj, result, _package = _handover(db, world, awarded=5_000, budget={})
    assert "approved_budget" in result.re_entered_fields
    assert any("no approved budget" in w for w in result.warnings)


def test_a_reduced_award_is_recorded_and_flagged(db, world):
    """An award smaller than the request is the common case and it changes the whole
    workplan, so it is recorded rather than discovered later."""
    _service_obj, result, _package = _handover(db, world, awarded=60_000_000)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()

    assert grant.size_relative_to_request == models.Grant.SIZE_REDUCED
    assert any("reduced" in w for w in result.warnings)
    assert any("must be revised" in w for w in result.warnings)


def test_an_award_matching_the_request_is_not_flagged(db, world):
    _service_obj, result, _package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()
    assert grant.size_relative_to_request == models.Grant.SIZE_AS_REQUESTED
    assert not any("reduced" in w for w in result.warnings)


# ===========================================================================
# A GRANT ONLY FOLLOWS AN APPLICATION THE FUNDER RECEIVED
# ===========================================================================
def test_an_unsubmitted_application_cannot_be_handed_over(db, world):
    """Handing over an application that was never filed would create a grant for money
    nobody agreed to give."""
    package = _submitted_package(db, world)
    package.status = models.SubmissionPackage.AUTHORISED
    db.commit()

    service = _service(db, world)
    with pytest.raises(NotDeliverable) as excinfo:
        service.handover(package=package, reference="X", awarded_amount=1000)
    assert excinfo.value.code == "APPLICATION_NOT_SUBMITTED"


def test_a_grant_requires_the_funders_reference(db, world):
    package = _submitted_package(db, world)
    service = _service(db, world)
    with pytest.raises(NotDeliverable) as excinfo:
        service.handover(package=package, reference="   ", awarded_amount=1000)
    assert excinfo.value.code == "NO_FUNDER_REFERENCE"


def test_a_zero_value_award_is_refused(db, world):
    """Almost always a parsing error rather than an award."""
    package = _submitted_package(db, world)
    service = _service(db, world)
    with pytest.raises(NotDeliverable) as excinfo:
        service.handover(package=package, reference="G-1", awarded_amount=0)
    assert excinfo.value.code == "AWARD_AMOUNT_INVALID"


def test_a_duplicate_grant_reference_is_refused(db, world):
    _service_obj, _result, _package = _handover(db, world, reference="SAME-REF")
    package = _submitted_package(db, world)
    service = _service(db, world)
    with pytest.raises(NotDeliverable) as excinfo:
        service.handover(package=package, reference="SAME-REF", awarded_amount=1000)
    assert excinfo.value.code == "GRANT_ALREADY_EXISTS"


# ===========================================================================
# CONDITIONS ARE NEVER SATISFIED BY INFERENCE
# ===========================================================================
def test_a_condition_cannot_be_satisfied_without_evidence(db, world):
    """A condition assumed met is discovered when a disbursement is withheld, at which
    point the funder's confidence has already been spent."""
    service, result, _package = _handover(db, world)
    condition = service.add_condition(
        grant_id=result.grant_id, title="Signed grant agreement",
        kind=models.GrantCondition.KIND_PRECONDITION,
    )
    db.commit()

    with pytest.raises(DeliveryError):
        service.satisfy_condition(condition_id=condition.id, evidence_ref=None)
    with pytest.raises(DeliveryError):
        service.satisfy_condition(condition_id=condition.id, evidence_ref="  ")
    assert condition.status == models.GrantCondition.STATUS_OPEN


def test_a_condition_with_evidence_is_satisfied(db, world):
    service, result, _package = _handover(db, world)
    condition = service.add_condition(
        grant_id=result.grant_id, title="Signed grant agreement",
        kind=models.GrantCondition.KIND_PRECONDITION,
    )
    db.commit()

    service.satisfy_condition(
        condition_id=condition.id, evidence_ref="vault://doc/signed-agreement.pdf"
    )
    db.commit()
    assert condition.status == models.GrantCondition.STATUS_SATISFIED
    assert condition.evidence_ref


def test_a_precondition_blocks_payment_by_default(db, world):
    """Defaults from the KIND rather than from the caller: letting a caller forget would
    silently make a precondition non-blocking."""
    service, result, _package = _handover(db, world)
    blocking = service.add_condition(
        grant_id=result.grant_id, title="Bank confirmation",
        kind=models.GrantCondition.KIND_FINANCIAL,
    )
    routine = service.add_condition(
        grant_id=result.grant_id, title="Send us a photo",
        kind=models.GrantCondition.KIND_OTHER,
    )
    db.commit()
    assert blocking.blocks_payment is True
    assert routine.blocks_payment is False


def test_a_caller_can_overrule_the_blocking_default(db, world):
    service, result, _package = _handover(db, world)
    condition = service.add_condition(
        grant_id=result.grant_id, title="Already handled elsewhere",
        kind=models.GrantCondition.KIND_PRECONDITION, blocks_payment=False,
    )
    db.commit()
    assert condition.blocks_payment is False


def test_waiving_is_distinct_from_satisfying(db, world):
    """'We decided it did not apply' is not evidence of compliance, so the two are
    different statuses."""
    service, result, _package = _handover(db, world)
    condition = service.add_condition(grant_id=result.grant_id, title="Condition")
    db.commit()

    service.waive_condition(condition_id=condition.id, reason="the funder agreed it did not apply")
    db.commit()
    assert condition.status == models.GrantCondition.STATUS_WAIVED
    assert condition.evidence_ref is None
    assert "waived" in condition.evidence_note


def test_waiving_requires_a_reason(db, world):
    service, result, _package = _handover(db, world)
    condition = service.add_condition(grant_id=result.grant_id, title="Condition")
    db.commit()
    with pytest.raises(DeliveryError):
        service.waive_condition(condition_id=condition.id, reason="")


# ===========================================================================
# REPORTING — WHERE MONEY IS LOST TO SILENCE
# ===========================================================================
def test_a_reporting_obligation_requires_a_due_date(db, world):
    """An unmonitored report is the one that gets missed."""
    service, result, _package = _handover(db, world)
    with pytest.raises(DeliveryError):
        service.add_reporting_obligation(
            grant_id=result.grant_id, title="Quarterly narrative", due_on=None
        )


def test_a_report_cannot_be_marked_submitted_without_a_reference(db, world):
    """Believing a report was filed when it was not is worse than knowing it is late,
    because late can still be fixed."""
    service, result, _package = _handover(db, world)
    obligation = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1 narrative", due_on=_now() + timedelta(days=10)
    )
    db.commit()
    with pytest.raises(DeliveryError):
        service.record_report_submitted(obligation_id=obligation.id, reference="")
    assert obligation.status == models.ReportingObligation.STATUS_PENDING


def test_a_report_is_submitted_with_the_funders_acknowledgement(db, world):
    service, result, _package = _handover(db, world)
    obligation = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1 narrative", due_on=_now() + timedelta(days=10)
    )
    db.commit()

    service.record_report_submitted(
        obligation_id=obligation.id, reference="ACK-Q1-2027",
        report_document_ref="vault://report/q1.pdf",
    )
    db.commit()
    assert obligation.status == models.ReportingObligation.STATUS_SUBMITTED
    assert obligation.reference == "ACK-Q1-2027"


def test_a_report_approaching_its_deadline_becomes_due_soon(db, world):
    """Statuses derive from the clock rather than from anyone remembering."""
    service, result, _package = _handover(db, world)
    obligation = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1 narrative", due_on=_now() + timedelta(days=5),
        remind_days_before=14,
    )
    db.commit()

    moved = service.refresh_reporting_statuses()
    db.commit()
    assert moved["due_soon"] == 1
    assert obligation.status == models.ReportingObligation.STATUS_DUE_SOON


def test_a_passed_report_deadline_becomes_overdue(db, world):
    service, result, _package = _handover(db, world)
    obligation = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1 narrative", due_on=_now() - timedelta(days=2)
    )
    db.commit()

    moved = service.refresh_reporting_statuses()
    db.commit()
    assert moved["overdue"] == 1
    assert obligation.status == models.ReportingObligation.STATUS_OVERDUE


def test_a_far_off_report_is_untouched(db, world):
    """Or everything would be 'due soon' and the signal would be worthless."""
    service, result, _package = _handover(db, world)
    obligation = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Final report", due_on=_now() + timedelta(days=300),
        remind_days_before=14,
    )
    db.commit()
    service.refresh_reporting_statuses()
    db.commit()
    assert obligation.status == models.ReportingObligation.STATUS_PENDING


def test_the_reminder_window_is_per_obligation(db, world):
    """A final audit needs more warning than a monthly update, so one global window
    would be wrong for one of them."""
    service, result, _package = _handover(db, world)
    monthly = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Monthly", due_on=_now() + timedelta(days=20),
        remind_days_before=7,
    )
    audit = service.add_reporting_obligation(
        grant_id=result.grant_id, title="Final audit", due_on=_now() + timedelta(days=20),
        remind_days_before=60,
    )
    db.commit()
    service.refresh_reporting_statuses()
    db.commit()

    assert monthly.status == models.ReportingObligation.STATUS_PENDING
    assert audit.status == models.ReportingObligation.STATUS_DUE_SOON


# ===========================================================================
# MONEY
# ===========================================================================
def test_a_disbursement_cannot_be_received_without_a_reference(db, world):
    """A project spending against a tranche it has not got is a project in trouble, and
    'it probably arrived' is how that happens."""
    service, result, _package = _handover(db, world)
    tranche = service.expect_disbursement(
        grant_id=result.grant_id, amount=60_000_000, expected_on=_now() + timedelta(days=7)
    )
    db.commit()

    with pytest.raises(DeliveryError):
        service.record_disbursement_received(disbursement_id=tranche.id, reference="")
    assert tranche.status == models.Disbursement.EXPECTED


def test_a_received_disbursement_records_the_reference(db, world):
    service, result, _package = _handover(db, world)
    tranche = service.expect_disbursement(grant_id=result.grant_id, amount=60_000_000)
    db.commit()

    service.record_disbursement_received(
        disbursement_id=tranche.id, reference="BANK-REF-991",
        amount_received=60_000_000,
    )
    db.commit()
    assert tranche.status == models.Disbursement.RECEIVED
    assert tranche.reference == "BANK-REF-991"


def test_a_short_payment_is_recorded_rather_than_lost(db, world):
    """A short receipt is real and common, and it is invisible if only the expected
    amount is kept."""
    service, result, _package = _handover(db, world)
    tranche = service.expect_disbursement(grant_id=result.grant_id, amount=60_000_000)
    db.commit()

    service.record_disbursement_received(
        disbursement_id=tranche.id, reference="BANK-1", amount_received=55_000_000
    )
    db.commit()

    assert tranche.amount_received == Decimal("55000000")
    assert "short" in (tranche.variance_note or "").lower()


def test_expected_and_received_are_separate_facts(db, world):
    """Collapsing them loses the ability to answer 'what is late', which is the only
    question that matters about a schedule."""
    service, result, _package = _handover(db, world)
    first = service.expect_disbursement(grant_id=result.grant_id, amount=50_000_000)
    second = service.expect_disbursement(grant_id=result.grant_id, amount=70_000_000)
    db.commit()
    service.record_disbursement_received(disbursement_id=first.id, reference="R-1")
    db.commit()

    assert first.status == models.Disbursement.RECEIVED
    assert second.status == models.Disbursement.EXPECTED


def test_a_cancelled_tranche_cannot_be_marked_received(db, world):
    service, result, _package = _handover(db, world)
    tranche = service.expect_disbursement(grant_id=result.grant_id, amount=1000)
    tranche.status = models.Disbursement.CANCELLED
    db.commit()
    with pytest.raises(DeliveryError):
        service.record_disbursement_received(disbursement_id=tranche.id, reference="R")


# ===========================================================================
# MONITORING
# ===========================================================================
def test_deadlines_gather_all_three_sources(db, world):
    """An organisation's obligations are not separated by which table they live in. A
    view showing only reports would hide the precondition blocking the next payment."""
    service, result, _package = _handover(db, world)
    service.add_condition(
        grant_id=result.grant_id, title="Bank confirmation",
        kind=models.GrantCondition.KIND_FINANCIAL, due_on=_now() + timedelta(days=3),
    )
    service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1 report", due_on=_now() + timedelta(days=10)
    )
    service.expect_disbursement(
        grant_id=result.grant_id, amount=1000, expected_on=_now() + timedelta(days=5)
    )
    db.commit()

    found = service.deadlines(within_days=30)
    kinds = {d.kind for d in found}
    assert kinds == {"CONDITION", "REPORT", "DISBURSEMENT"}
    # Soonest first, because that is the order they need attention.
    assert [d.days_remaining for d in found] == sorted(d.days_remaining for d in found)


def test_deadlines_respect_the_horizon(db, world):
    service, result, _package = _handover(db, world)
    service.add_reporting_obligation(
        grant_id=result.grant_id, title="Far future", due_on=_now() + timedelta(days=200)
    )
    db.commit()
    assert service.deadlines(within_days=30) == []


def test_the_compliance_summary_separates_the_three_risks(db, world):
    """Three lists rather than one score: a score would require weighting a blocked
    payment against a late report, and the right response to each is different - one is
    a phone call, the other is writing."""
    service, result, _package = _handover(db, world)
    service.add_condition(
        grant_id=result.grant_id, title="Blocking", kind=models.GrantCondition.KIND_LEGAL,
        due_on=_now() - timedelta(days=1),
    )
    service.add_reporting_obligation(
        grant_id=result.grant_id, title="Late report", due_on=_now() - timedelta(days=3)
    )
    service.expect_disbursement(
        grant_id=result.grant_id, amount=5000, expected_on=_now() - timedelta(days=4)
    )
    db.commit()

    summary = service.compliance_summary()
    assert summary["counts"]["blocking_conditions"] == 1
    assert summary["counts"]["overdue_reports"] == 1
    assert summary["counts"]["late_disbursements"] == 1
    assert summary["overdue_reports"][0]["days_late"] == 3


def test_the_portfolio_shows_money_actually_in_the_bank(db, world):
    """So 'how much of this grant have we got' is answerable rather than assumed."""
    service, result, _package = _handover(db, world)
    a = service.expect_disbursement(grant_id=result.grant_id, amount=50_000_000)
    service.expect_disbursement(grant_id=result.grant_id, amount=70_000_000)
    db.commit()
    service.record_disbursement_received(disbursement_id=a.id, reference="R-1")
    db.commit()

    portfolio = service.compliance_summary()["portfolio"]
    # Two decimal places: these come back from a Numeric(18, 2) column, and money
    # printed without its minor units is how a rounded figure gets mistaken for a
    # precise one.
    assert portfolio["scheduled_total"] == "120000000.00"
    assert portfolio["received_total"] == "50000000.00"
    assert portfolio["outstanding_total"] == "70000000.00"


def test_a_satisfied_condition_leaves_the_blocking_list(db, world):
    service, result, _package = _handover(db, world)
    condition = service.add_condition(
        grant_id=result.grant_id, title="Bank letter",
        kind=models.GrantCondition.KIND_FINANCIAL,
    )
    db.commit()
    assert service.compliance_summary()["counts"]["blocking_conditions"] == 1

    service.satisfy_condition(condition_id=condition.id, evidence_ref="vault://bank.pdf")
    db.commit()
    assert service.compliance_summary()["counts"]["blocking_conditions"] == 0


# ===========================================================================
# A GRANT IS CLOSED, NEVER DELETED
# ===========================================================================
def test_a_grant_is_terminated_not_deleted(db, world):
    """DELETE is revoked on every table in this phase. Deleting destroys the record that
    money was expected and why it stopped - precisely what an auditor asks for."""
    service, result, _package = _handover(db, world)
    service.terminate_grant(
        grant_id=result.grant_id, status=models.Grant.STATUS_TERMINATED,
        reason="the funder withdrew after a change of strategy",
    )
    db.commit()

    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()
    assert grant.status == models.Grant.STATUS_TERMINATED


def test_closing_a_grant_requires_a_reason(db, world):
    service, result, _package = _handover(db, world)
    with pytest.raises(DeliveryError):
        service.terminate_grant(
            grant_id=result.grant_id, status=models.Grant.STATUS_COMPLETED, reason=""
        )


def test_an_invalid_closing_status_is_refused(db, world):
    service, result, _package = _handover(db, world)
    with pytest.raises(DeliveryError):
        service.terminate_grant(
            grant_id=result.grant_id, status="WHATEVER", reason="because"
        )


# ===========================================================================
# TENANCY
# ===========================================================================
def test_another_organisations_grant_is_invisible(db, world):
    _service_obj, result, _package = _handover(db, world)
    other_org, other_agent_service = _org_and_agent(db, name="Other NGO")
    other_agent = other_agent_service.get()

    service = DeliveryService(db, org_id=other_org.id, agent_id=other_agent.id)
    with pytest.raises(DeliveryError):
        service.add_condition(grant_id=result.grant_id, title="Not mine")


def test_a_package_from_another_organisation_cannot_be_handed_over(db, world):
    package = _submitted_package(db, world)
    other_org, other_agent_service = _org_and_agent(db, name="Other NGO")
    other_agent = other_agent_service.get()

    service = DeliveryService(db, org_id=other_org.id, agent_id=other_agent.id)
    with pytest.raises(DeliveryError):
        service.handover(package=package, reference="X", awarded_amount=1000)


# ===========================================================================
# THE HANDOVER IS OBSERVABLE
# ===========================================================================
def test_the_handover_emits_the_expected_events(db, world):
    service, result, _package = _handover(db, world)
    service.add_condition(grant_id=result.grant_id, title="Bank letter")
    service.add_reporting_obligation(
        grant_id=result.grant_id, title="Q1", due_on=_now() + timedelta(days=30)
    )
    service.expect_disbursement(grant_id=result.grant_id, amount=1000)
    db.commit()

    events = db.query(models.OutboxEvent).all()
    names = {e.event_type for e in events}
    assert "granada:v1:award.recorded" in names
    assert "granada:v1:award.condition_added" in names
    assert "granada:v1:disbursement.expected" in names


def test_the_handover_records_activity_for_the_organisation(db, world):
    """Never an anonymous handover: the audit trail must say this was the agent's work."""
    org, agent, _application = world
    _service_obj, result, _package = _handover(db, world)

    activity = db.query(models.AgentActivity).filter(
        models.AgentActivity.summary_key == "award.recorded"
    ).one()
    assert activity.agent_id == agent.id
    assert activity.org_id == org.id
    assert activity.visibility == models.AgentActivity.VISIBILITY_CUSTOMER


def test_a_full_schedule_can_be_created_in_one_handover(db, world):
    """The realistic case: the award letter arrives with conditions, a reporting
    calendar and a payment schedule, and all of it becomes tracked objects at once."""
    package = _submitted_package(db, world)
    service = _service(db, world)
    result = service.handover(
        package=package,
        reference="GRANT-FULL-1",
        awarded_amount=120_000_000,
        conditions=[
            {"title": "Signed agreement", "kind": models.GrantCondition.KIND_PRECONDITION,
             "due_on": _now() + timedelta(days=14)},
            {"title": "Safeguarding policy", "kind": models.GrantCondition.KIND_SAFEGUARDING},
        ],
        reporting_schedule=[
            {"title": "Q1 narrative", "due_on": _now() + timedelta(days=90),
             "kind": models.ReportingObligation.KIND_NARRATIVE,
             "period": models.ReportingObligation.PERIOD_QUARTERLY},
            {"title": "Annual audit", "due_on": _now() + timedelta(days=365),
             "kind": models.ReportingObligation.KIND_AUDIT,
             "period": models.ReportingObligation.PERIOD_ANNUAL,
             "remind_days_before": 60},
        ],
        disbursement_schedule=[
            {"amount": 60_000_000, "label": "First tranche",
             "expected_on": _now() + timedelta(days=30), "tranche_number": 1},
            {"amount": 60_000_000, "label": "Final tranche",
             "expected_on": _now() + timedelta(days=300), "tranche_number": 2},
        ],
    )
    db.commit()

    assert len(result.conditions) == 2
    assert len(result.obligations) == 2
    assert len(result.disbursements) == 2
    assert result.re_entered_fields == []
    assert db.query(models.GrantCondition).count() == 2
    assert db.query(models.ReportingObligation).count() == 2
    assert db.query(models.Disbursement).count() == 2
