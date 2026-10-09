"""The submission lifecycle. Almost every test here is about NOT retrying a possible submission.

The bug this module prevents is not a crash. It is that a crash *after* the click leaves no evidence
either way, and naive retry logic reads "no receipt" as "did not submit" - filing the same
application with the same funder twice.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.submission_lifecycle import (  # noqa: E402
    AUTHORISED,
    BLOCKED,
    BROWSER_EXECUTING,
    FAILED,
    FORM_VALIDATED,
    PACKAGE_READY,
    RECONCILIATION_WINDOW,
    SUBMISSION_PENDING,
    SUBMITTED,
    UNCERTAIN,
    LifecycleError,
    SubmissionRun,
    advance,
    describe,
    may_retry,
    next_action,
    reconcile,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def run_at(state: str, **over) -> SubmissionRun:
    base = dict(
        run_id="run-1",
        org_id="org-aaaa",
        package_id="pkg-1",
        package_fingerprint="fp-1",
        state=state,
    )
    base.update(over)
    return SubmissionRun(**base)  # type: ignore[arg-type]


def to_pending(**over) -> SubmissionRun:
    r = run_at(AUTHORISED, **over)
    advance(r, BROWSER_EXECUTING, now=NOW)
    advance(r, FORM_VALIDATED, now=NOW)
    advance(r, SUBMISSION_PENDING, now=NOW)
    return r


# ===========================================================================
# THE CRASH-AFTER-CLICK CASE
# ===========================================================================
def test_pending_cannot_be_retried_in_place():
    """THE test. A browser that died after clicking Submit leaves the application in a state where a
    retry may file a second application."""
    r = to_pending()
    with pytest.raises(LifecycleError) as e:
        advance(r, BROWSER_EXECUTING)
    assert "second application" in str(e.value)
    assert "reconcile" in str(e.value)


def test_pending_is_not_reported_as_a_retry():
    """`next_action` must never say retry from PENDING - that instruction is the bug."""
    d = next_action(to_pending(), now=NOW)
    assert d["action"] == "reconcile"
    assert "resubmit" in d["must_not"]
    assert "mark_submitted_without_receipt" in d["must_not"]


def test_an_absent_confirmation_is_uncertain_not_failed():
    """The directive's rule, at the reconciliation site: no evidence either way must not be recorded
    as failure, because failure is what authorises a retry."""
    r = reconcile(to_pending(), now=NOW + timedelta(minutes=5))
    assert r.state == UNCERTAIN
    assert r.state != FAILED
    assert "not the same as failure" in (r.reason or "")


def test_uncertain_must_not_be_retried_either():
    r = reconcile(to_pending())
    assert may_retry(r) is False
    with pytest.raises(LifecycleError):
        advance(r, BROWSER_EXECUTING)
    assert next_action(r)["must_not"] == ["resubmit"]


def test_reconciliation_can_confirm_success():
    r = reconcile(to_pending(), external_receipt="FUNDER-REF-9")
    assert r.state == SUBMITTED
    assert r.receipt == "FUNDER-REF-9"


def test_reconciliation_can_confirm_the_attempt_never_landed():
    """The one path that frees the application to be attempted again - and it requires positive
    evidence that nothing was received, not merely the absence of a receipt."""
    r = reconcile(to_pending(), confirmed_not_received=True)
    assert r.state == FAILED
    assert "no record of this application" in (r.reason or "")


# ===========================================================================
# SUBMITTED REQUIRES A RECEIPT
# ===========================================================================
def test_submitted_without_a_receipt_is_refused():
    """No sequence of legitimate calls may mark an application submitted on the strength of a browser
    reporting success. The receipt is the only evidence that counts."""
    r = run_at(FORM_VALIDATED)
    advance(r, SUBMISSION_PENDING)
    with pytest.raises(LifecycleError) as e:
        advance(r, SUBMITTED)
    assert "confirmed external evidence" in str(e.value)


def test_a_receipt_already_on_the_run_is_sufficient():
    """Recovery case: the receipt was recorded before the crash, so marking submitted afterwards does
    not need it passed again."""
    r = run_at(SUBMISSION_PENDING, receipt="FUNDER-REF-1")
    advance(r, SUBMITTED)
    assert r.state == SUBMITTED


def test_submitted_is_terminal():
    r = run_at(SUBMISSION_PENDING, receipt="R")
    advance(r, SUBMITTED)
    with pytest.raises(LifecycleError):
        advance(r, BROWSER_EXECUTING)
    assert next_action(r)["action"] == "none"


# ===========================================================================
# FAILED is the ONLY retryable state
# ===========================================================================
def test_only_a_technical_failure_may_be_retried():
    for state in (PACKAGE_READY, AUTHORISED, BROWSER_EXECUTING, FORM_VALIDATED, SUBMISSION_PENDING):
        assert may_retry(run_at(state)) is False, f"{state} must not be retryable in place"
    assert may_retry(run_at(FAILED)) is True


def test_failure_before_the_click_offers_a_new_run_not_a_rewind():
    r = run_at(BROWSER_EXECUTING, reason="navigation failed")
    advance(r, FAILED)
    d = next_action(r)
    assert d["action"] == "start_new_run"


# ===========================================================================
# BLOCKED is not FAILED
# ===========================================================================
def test_blocked_awaits_information_and_does_not_schedule_a_retry():
    """The directive's distinction: a missing registration certificate is not an infrastructure
    error, and must not be retried."""
    r = run_at(PACKAGE_READY, reason="verified registration certificate missing")
    advance(r, BLOCKED)
    d = next_action(r)
    assert d["action"] == "await_information"
    assert "schedule_retry" in d["must_not"]
    assert "registration certificate" in d["reason"]


def test_blocked_can_resume_when_information_arrives():
    r = run_at(PACKAGE_READY)
    advance(r, BLOCKED, reason="missing budget")
    advance(r, AUTHORISED)
    assert r.state == AUTHORISED


# ===========================================================================
# The full path
# ===========================================================================
def test_the_documented_lifecycle_walks_end_to_end():
    r = run_at(PACKAGE_READY)
    advance(r, AUTHORISED, reason="human approved")
    advance(r, BROWSER_EXECUTING)
    advance(r, FORM_VALIDATED, evidence=["evidence/shot-1.png"])
    advance(r, SUBMISSION_PENDING)
    advance(r, SUBMITTED, receipt="FUNDER-REF-42")
    assert r.receipt == "FUNDER-REF-42"
    assert r.evidence == ["evidence/shot-1.png"]


def test_an_illegal_move_names_the_legal_ones():
    r = run_at(PACKAGE_READY)
    with pytest.raises(LifecycleError) as e:
        advance(r, SUBMITTED)
    assert AUTHORISED in str(e.value)


def test_a_repeated_request_for_the_current_state_is_idempotent():
    """Redelivery must not corrupt a run."""
    r = run_at(BROWSER_EXECUTING)
    assert advance(r, BROWSER_EXECUTING) is r


def test_an_unknown_state_is_refused():
    with pytest.raises(LifecycleError):
        advance(run_at(PACKAGE_READY), "MOSTLY_DONE")


# ===========================================================================
# Escalation
# ===========================================================================
def test_a_long_pending_run_escalates_rather_than_being_retried():
    r = to_pending()
    d = next_action(r, now=NOW + RECONCILIATION_WINDOW + timedelta(minutes=1))
    assert d["action"] == "reconcile"
    assert d["escalate"] is True
    assert "resubmit" in d["must_not"]


def test_a_short_pending_run_does_not_escalate_yet():
    d = next_action(to_pending(), now=NOW + timedelta(minutes=1))
    assert d["escalate"] is False


# ===========================================================================
# The boundary is stated
# ===========================================================================
def test_describe_states_that_it_cannot_reach_submitted_alone():
    d = describe()
    assert d["retryable_in_place"] == [FAILED]
    assert SUBMISSION_PENDING in d["never_retried_in_place"]
    assert UNCERTAIN in d["never_retried_in_place"]
    assert d["receipt_required_for"] == [SUBMITTED]
    assert "cannot_reach_submitted_on_its_own" in d


def test_describe_does_not_claim_to_replace_the_existing_gates():
    joined = " ".join(describe()["does_not_replace"])
    assert "REQUIRES_APPROVAL" in joined
    assert "REQUIRES_RECEIPT" in joined
    assert "readiness" in joined
    assert "submission_authority" in joined
