"""The submission lifecycle, and the one state that stops Granada filing twice.

WHY THIS IS A MODULE AND NOT A STATUS COLUMN
--------------------------------------------
The directive's hardest requirement in this section is not a transition, it is a *refusal to
conclude*:

    "If a browser crashes after clicking Submit, Granada must not automatically repeat the submission
     without determining whether the first attempt succeeded."
    "A missing confirmation must be treated as an uncertain outcome, not automatically as failure."
    "Never mark an application SUBMITTED unless valid external confirmation evidence has been
     obtained."

Every naive submission driver has the same bug, and it is not a crash: it is that a crash *after*
the click leaves no evidence either way, and the retry logic then treats "no receipt" as "did not
submit". That turns a transient browser failure into a duplicate application filed with a funder -
the one error a grant applicant cannot undo.

So `SUBMISSION_PENDING` is not a waiting state. It is a statement that a click MAY have landed and
nobody knows. `next_action` refuses to retry from it, and refuses to mark it submitted without a
receipt.

WHAT THIS DOES NOT DO
---------------------
It does not submit anything, does not talk to a browser, and does not replace the existing gates.
`Workspace.REQUIRES_APPROVAL`, `Workspace.REQUIRES_RECEIPT` and the readiness check all still apply;
this module models the browser-side lifecycle that those gates bound, and it is deliberately
incapable of reaching `SUBMITTED` on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
PACKAGE_READY = "PACKAGE_READY"
AUTHORISED = "AUTHORISED"
BROWSER_EXECUTING = "BROWSER_EXECUTING"
FORM_VALIDATED = "FORM_VALIDATED"
#: The click may have landed. Not a wait - an absence of knowledge.
SUBMISSION_PENDING = "SUBMISSION_PENDING"
SUBMITTED = "SUBMITTED"

#: Outcomes that are neither success nor failure, and must never be collapsed into either.
BLOCKED = "BLOCKED"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"

#: The linear path an application walks when nothing goes wrong.
LIFECYCLE = (
    PACKAGE_READY,
    AUTHORISED,
    BROWSER_EXECUTING,
    FORM_VALIDATED,
    SUBMISSION_PENDING,
    SUBMITTED,
)

ALL_STATES = frozenset(LIFECYCLE) | frozenset({BLOCKED, FAILED, UNCERTAIN})

TERMINAL = frozenset({SUBMITTED, FAILED})

#: Legal moves. Deliberately does NOT include SUBMISSION_PENDING -> BROWSER_EXECUTING, which is the
#: illegal retry this module exists to prevent.
ALLOWED: dict[str, frozenset[str]] = {
    PACKAGE_READY: frozenset({AUTHORISED, BLOCKED, FAILED}),
    AUTHORISED: frozenset({BROWSER_EXECUTING, BLOCKED, FAILED}),
    BROWSER_EXECUTING: frozenset({FORM_VALIDATED, SUBMISSION_PENDING, FAILED, BLOCKED}),
    FORM_VALIDATED: frozenset({SUBMISSION_PENDING, FAILED}),
    # From "we may have submitted": the ONLY exits are a verified receipt, or a human/scheduled
    # reconciliation that establishes what happened. Never a retry.
    SUBMISSION_PENDING: frozenset({SUBMITTED, UNCERTAIN, FAILED}),
    SUBMITTED: frozenset(),
    UNCERTAIN: frozenset({SUBMISSION_PENDING, FAILED}),
    BLOCKED: frozenset({PACKAGE_READY, AUTHORISED}),
    FAILED: frozenset({PACKAGE_READY}),
}

#: How long an application may sit in SUBMISSION_PENDING before it is escalated rather than retried.
RECONCILIATION_WINDOW = timedelta(hours=24)


class LifecycleError(Exception):
    """An illegal move. Raised rather than logged, because the caller asked for something it must
    not do and must be told."""


@dataclass
class SubmissionRun:
    """One attempt to submit one package version.

    `state` is the lifecycle position. `evidence` holds what actually happened, which is what
    distinguishes SUBMITTED from SUBMISSION_PENDING.
    """

    run_id: str
    org_id: str
    package_id: str
    package_fingerprint: str
    state: str = PACKAGE_READY
    #: The external receipt. The ONLY thing that legitimises SUBMITTED.
    receipt: Optional[str] = None
    #: Screenshots, form-validation output, uploaded-document names. References, never contents and
    #: never raw paths.
    evidence: list[str] = field(default_factory=list)
    #: Every recoverable problem and what was done about it, so a retry that eventually succeeded can
    #: be told apart from one that never had a problem.
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pending_since: Optional[datetime] = None
    reason: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def advance(
    run: SubmissionRun,
    to_state: str,
    *,
    reason: Optional[str] = None,
    receipt: Optional[str] = None,
    evidence: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> SubmissionRun:
    """Move the run, or refuse.

    The two guards that matter are here rather than in the caller: SUBMITTED requires a receipt, and
    SUBMISSION_PENDING cannot go back to executing.
    """
    if to_state not in ALL_STATES:
        raise LifecycleError(f"unknown state {to_state!r}")
    if to_state == run.state:
        return run

    allowed = ALLOWED.get(run.state, frozenset())
    if to_state not in allowed:
        if run.state == SUBMISSION_PENDING and to_state == BROWSER_EXECUTING:
            # Named specifically: this is the duplicate-filing bug, and a generic "illegal
            # transition" would hide why it matters.
            raise LifecycleError(
                "an application in SUBMISSION_PENDING may have already been submitted; "
                "re-running the browser from here risks filing a second application - reconcile "
                "the first attempt instead"
            )
        raise LifecycleError(
            f"{run.state} -> {to_state} is not permitted; from {run.state} the legal states are "
            f"{sorted(allowed) or ['none (terminal)']}"
        )

    moment = now or _now()

    if to_state == SUBMITTED:
        # THE rule. No receipt, no submission - regardless of what the browser reported.
        if not (receipt or run.receipt):
            raise LifecycleError(
                "SUBMITTED requires confirmed external evidence; without a receipt this is an "
                "uncertain outcome, not a submission"
            )

    if to_state == SUBMISSION_PENDING:
        run.pending_since = moment

    if evidence:
        run.evidence.extend(evidence)
    if receipt:
        run.receipt = receipt
    run.state = to_state
    if reason is not None:
        run.reason = reason
    return run


def next_action(run: SubmissionRun, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """What should happen next, and - more importantly - what must not.

    Returns a description rather than performing anything: the caller decides, and this function has
    no browser and no database.
    """
    moment = now or _now()

    if run.state == SUBMITTED:
        return {"action": "none", "reason": "already submitted", "receipt": run.receipt}

    if run.state == SUBMISSION_PENDING:
        waited = (moment - run.pending_since) if run.pending_since else timedelta(0)
        return {
            # NOT "retry". The first attempt may have succeeded.
            "action": "reconcile",
            "reason": (
                "a submission may already have been made and no confirmation was observed; "
                "determine whether the first attempt succeeded before doing anything else"
            ),
            "waited_seconds": int(waited.total_seconds()),
            "escalate": waited >= RECONCILIATION_WINDOW,
            "must_not": ["resubmit", "mark_submitted_without_receipt"],
        }

    if run.state == UNCERTAIN:
        return {
            "action": "await_human",
            "reason": (
                "reconciliation could not establish whether the application was received; this "
                "needs a human to check the funder's records rather than another attempt"
            ),
            "must_not": ["resubmit"],
        }

    if run.state == BLOCKED:
        return {
            "action": "await_information",
            "reason": run.reason or "the package is missing required information",
            "must_not": ["resubmit", "schedule_retry"],
        }

    if run.state == FAILED:
        # A genuine technical failure - the browser never reached the click - is the ONLY state from
        # which a retry is safe, and it must be a fresh run rather than a rewind of this one.
        return {"action": "start_new_run", "reason": run.reason or "the previous attempt failed"}

    return {"action": "continue", "reason": f"proceed from {run.state}"}


def reconcile(
    run: SubmissionRun,
    *,
    external_receipt: Optional[str] = None,
    confirmed_not_received: bool = False,
    now: Optional[datetime] = None,
) -> SubmissionRun:
    """Resolve a SUBMISSION_PENDING run once someone has established what actually happened.

    Both outcomes are legitimate. What is NOT legitimate is resolving it by trying again - which is
    why neither branch here starts a browser.
    """
    if run.state != SUBMISSION_PENDING:
        raise LifecycleError(f"reconciliation applies to {SUBMISSION_PENDING}, not {run.state}")

    if external_receipt:
        return advance(
            run, SUBMITTED, receipt=external_receipt, reason="reconciled: the funder confirmed receipt"
        )

    if confirmed_not_received:
        # The attempt demonstrably did not land. Back to PENDING would be wrong (nothing is pending)
        # and straight to a retry would be wrong (the caller must open a NEW run and re-authorise).
        return advance(
            run,
            FAILED,
            reason="reconciled: the receiving system has no record of this application",
            now=now,
        )

    # Neither confirmed nor denied: the honest answer, and the one a naive driver replaces with a
    # retry.
    return advance(
        run,
        UNCERTAIN,
        reason=(
            "reconciliation was inconclusive; the outcome remains unknown, which is not the same as "
            "failure and must not be treated as permission to try again"
        ),
        now=now,
    )


def may_retry(run: SubmissionRun) -> bool:
    """Whether this run may be attempted again, in place.

    True only where the browser never reached the point of a possible submission.
    """
    return run.state == FAILED


def describe() -> dict[str, Any]:
    """The boundary, stated for a reviewer."""
    return {
        "lifecycle": list(LIFECYCLE),
        "terminal": sorted(TERMINAL),
        "non_terminal_outcomes": [BLOCKED, FAILED, UNCERTAIN],
        "retryable_in_place": [FAILED],
        "never_retried_in_place": [SUBMISSION_PENDING, UNCERTAIN],
        "receipt_required_for": [SUBMITTED],
        "reconciliation_window_hours": RECONCILIATION_WINDOW.total_seconds() / 3600,
        "does_not_replace": [
            "Workspace.REQUIRES_APPROVAL",
            "Workspace.REQUIRES_RECEIPT",
            "readiness",
            "agent.submission_authority (explicit authority before any attempt)",
        ],
        "cannot_reach_submitted_on_its_own": (
            "advance() refuses SUBMITTED without a receipt, so no sequence of legitimate calls can "
            "mark an application submitted on the strength of a browser reporting success"
        ),
    }
