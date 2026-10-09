"""Recovery and failure classification.

The distinction under test: missing business information is the ORGANISATION's to supply; a technical
failure is GRANADA's to fix. They look identical in a log and could not be more different in
consequence - collapsing them asks an NGO to resubmit a document it already provided while presenting
a genuine crash as the organisation's problem.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.recovery import (  # noqa: E402
    PARKING_RESPONSES,
    RESPONSES,
    Diagnosis,
    FailureClass,
    RecoveryController,
    Response,
    describe,
    diagnose,
    from_page_evidence,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# THE DISTINCTION
# ===========================================================================
def test_missing_information_is_the_organisations_to_fix_and_is_never_retried():
    """THE test. A missing registration certificate is not an infrastructure error: it needs a human,
    it must not be retried, and the workflow must park."""
    d = diagnose(
        failure=FailureClass.MISSING_BUSINESS_INFORMATION,
        missing=["verified registration certificate"],
        now=NOW,
    )
    assert d.is_organisations_to_fix is True
    assert d.organisation_actionable is True
    assert d.retryable is False
    assert d.parks is True
    assert d.response is Response.AWAIT_ORGANISATION


def test_a_technical_failure_is_NOT_the_organisations_problem():
    d = diagnose(failure=FailureClass.TRANSIENT_TECHNICAL, detail="502", now=NOW)
    assert d.is_organisations_to_fix is False
    assert d.organisation_actionable is False
    assert d.retryable is True
    assert d.parks is False


def test_an_unnamed_missing_information_diagnosis_is_REFUSED():
    """An unnamed blocker is not actionable and would be indistinguishable from a technical failure -
    the earlier directive asked for the exact missing requirement, not just a blocked state."""
    with pytest.raises(ValueError) as e:
        diagnose(failure=FailureClass.MISSING_BUSINESS_INFORMATION)
    assert "must name what is missing" in str(e.value)


def test_the_missing_list_is_carried_as_structure_not_only_a_message():
    d = diagnose(
        failure=FailureClass.MISSING_BUSINESS_INFORMATION,
        missing=["budget_total", "registration_number"],
    )
    assert d.missing == ["budget_total", "registration_number"]


# ===========================================================================
# RESPONSES ARE DERIVED, NOT DECIDED AD HOC
# ===========================================================================
def test_every_class_has_a_permitted_response():
    """A class with no response would be handled differently at each call site."""
    for c in FailureClass:
        assert c in RESPONSES, f"{c} has no permitted response"


def test_a_403_is_STOP_not_retry():
    """§9: an access-denied challenge is an access-control checkpoint. Retrying a 403 is ignoring what
    the site said."""
    d = from_page_evidence(status_code=403)
    assert d.failure is FailureClass.ACCESS_DENIED
    assert d.response is Response.STOP
    assert d.retryable is False


def test_a_captcha_awaits_a_human_and_is_never_retried():
    d = from_page_evidence(challenge_present=True)
    assert d.failure is FailureClass.HUMAN_VERIFICATION_REQUIRED
    assert d.response is Response.AWAIT_HUMAN
    assert d.organisation_actionable is False, "a CAPTCHA is not the NGO's information problem"


def test_an_uncertain_submission_reconciles_rather_than_retries():
    d = diagnose(failure=FailureClass.UNCERTAIN_SUBMISSION, detail="submit did not confirm")
    assert d.response is Response.RECONCILE
    assert d.retryable is False
    assert d.parks is True


def test_an_undiagnosed_failure_stops():
    """You cannot retry what you have not diagnosed."""
    assert diagnose(failure=FailureClass.UNKNOWN).response is Response.STOP


def test_a_layout_change_reobserves_rather_than_restarting():
    """Re-observe, not re-run: the page moved, and restarting would repeat completed work."""
    d = from_page_evidence(layout_unrecognised=True)
    assert d.failure is FailureClass.LAYOUT_CHANGED
    assert d.response is Response.REOBSERVE
    assert d.retryable is True


def test_a_5xx_is_transient():
    assert from_page_evidence(status_code=503).failure is FailureClass.TRANSIENT_TECHNICAL


def test_a_timeout_is_transient():
    assert from_page_evidence(timed_out=True).failure is FailureClass.TRANSIENT_TECHNICAL


# ===========================================================================
# ORDERING OF EVIDENCE
# ===========================================================================
def test_a_challenge_outranks_a_validation_message():
    """A form that cannot be reached cannot be corrected, so the challenge is the real diagnosis."""
    d = from_page_evidence(challenge_present=True, validation_messages=["Amount invalid"])
    assert d.failure is FailureClass.HUMAN_VERIFICATION_REQUIRED


def test_a_lapsed_session_outranks_validation():
    d = from_page_evidence(login_present=True, validation_messages=["Required"])
    assert d.failure is FailureClass.SESSION_EXPIRED


def test_missing_facts_outrank_the_pages_validation_message():
    """If Granada never held the value, the page rejecting it is a SYMPTOM. Reporting the symptom
    would send the NGO to fix the wrong thing."""
    d = from_page_evidence(
        validation_messages=["Registration number is invalid"],
        missing_facts=["registration_number"],
    )
    assert d.failure is FailureClass.MISSING_BUSINESS_INFORMATION
    assert d.missing == ["registration_number"]


def test_a_validation_rejection_with_no_missing_fact_is_correctable():
    """Granada HAD a value and the page refused it - the value is wrong, not absent."""
    d = from_page_evidence(validation_messages=["Amount exceeds the ceiling"])
    assert d.failure is FailureClass.VALIDATION_REJECTED
    assert d.response is Response.CORRECT_AND_RETRY
    assert d.is_organisations_to_fix is False


# ===========================================================================
# BOUNDS AND LOOPS
# ===========================================================================
def test_a_retryable_failure_stops_at_its_bound():
    c = RecoveryController(max_attempts_per_class=2)
    d = diagnose(failure=FailureClass.TRANSIENT_TECHNICAL)
    for _ in range(2):
        ok, _why = c.may_attempt(d)
        assert ok is True
        c.record(d)
    ok, why = c.may_attempt(d)
    assert ok is False
    assert "bound is 2" in why


def test_a_parking_diagnosis_is_refused_by_the_controller_too():
    """Belt and braces: a caller that ignores `parks` is still stopped."""
    c = RecoveryController()
    d = diagnose(failure=FailureClass.MISSING_BUSINESS_INFORMATION, missing=["certificate"])
    ok, why = c.may_attempt(d)
    assert ok is False
    assert "not a retry" in why


def test_the_total_attempt_bound_is_enforced():
    c = RecoveryController(max_attempts_per_class=100, max_total_attempts=3)
    for i in range(3):
        c.record(diagnose(failure=FailureClass.TRANSIENT_TECHNICAL if i % 2 else FailureClass.LAYOUT_CHANGED))
    ok, why = c.may_attempt(diagnose(failure=FailureClass.TRANSIENT_TECHNICAL))
    assert ok is False
    assert "recovery attempts" in why


def test_an_alternating_loop_is_detected_even_under_every_per_class_bound():
    """Two alternating classes can stay under each per-class bound while making no progress."""
    c = RecoveryController(max_attempts_per_class=10, max_total_attempts=100)
    for _ in range(2):
        c.record(diagnose(failure=FailureClass.TRANSIENT_TECHNICAL))
        c.record(diagnose(failure=FailureClass.LAYOUT_CHANGED))
    assert c.loop_detected() is True


def test_making_progress_is_not_a_loop():
    c = RecoveryController()
    for f in (FailureClass.TRANSIENT_TECHNICAL, FailureClass.LAYOUT_CHANGED, FailureClass.SESSION_EXPIRED):
        c.record(diagnose(failure=f))
    assert c.loop_detected() is False


def test_the_summary_reports_attempts_by_class():
    c = RecoveryController()
    c.record(diagnose(failure=FailureClass.TRANSIENT_TECHNICAL))
    c.record(diagnose(failure=FailureClass.TRANSIENT_TECHNICAL))
    c.record(diagnose(failure=FailureClass.LAYOUT_CHANGED))
    s = c.summary()
    assert s["attempts"] == 3
    assert s["by_failure"]["TRANSIENT_TECHNICAL"] == 2


# ===========================================================================
# PARKING IS WHAT STOPS THE OLD RUNAWAY LOOP
# ===========================================================================
def test_parking_responses_cover_everything_that_cannot_be_retried():
    """A workflow parks when nothing Granada can do will change the outcome. The earlier directive's
    runaway-loop defect was WAITING workflows being rescheduled with no new information."""
    assert Response.AWAIT_ORGANISATION in PARKING_RESPONSES
    assert Response.AWAIT_HUMAN in PARKING_RESPONSES
    assert Response.STOP in PARKING_RESPONSES
    assert Response.RECONCILE in PARKING_RESPONSES
    assert Response.RETRY_BOUNDED not in PARKING_RESPONSES


def test_no_retryable_response_is_a_parking_response():
    """The two sets must not intersect, or parking and retry would contradict each other."""
    for c, r in RESPONSES.items():
        if r in PARKING_RESPONSES:
            assert not Diagnosis(failure=c, response=r, detail="").retryable, c


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_distinction_and_the_never_retried_set():
    d = describe()
    assert "organisation's to supply" in d["the_distinction"]
    assert "Granada's to fix" in d["the_distinction"]
    for f in (
        "MISSING_BUSINESS_INFORMATION",
        "HUMAN_VERIFICATION_REQUIRED",
        "ACCESS_DENIED",
        "UNCERTAIN_SUBMISSION",
        "UNKNOWN",
    ):
        assert f in d["never_retried"]
    joined = " ".join(d["does_not_do"])
    assert "browser_runtime" in joined
    assert "submission_lifecycle" in joined
