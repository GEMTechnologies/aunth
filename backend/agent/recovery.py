"""Diagnosing a failure, and the one distinction that keeps going wrong.

THE DISTINCTION
---------------
"Do not confuse missing business information with a technical system failure."

It appears in this directive and in the one before it, phrased as "a missing registration certificate
is not an infrastructure error", and it keeps being stated because the two look identical in a log:
something did not complete. They could not be more different in consequence.

A MISSING FACT is the organisation's to supply. It needs a human, it must not be retried, and the
workflow should park. Retrying it burns jobs, model calls and notifications on a question that no
amount of retrying can answer.

A TECHNICAL FAILURE is Granada's to fix. It is retryable, often transient, and should not be escalated
to the NGO, who can do nothing about it.

Collapsing them produces both failure modes at once: an NGO asked to resubmit a document it already
provided, while a genuine crash is presented as the organisation's problem. So `FailureClass` separates
them, and every class names its permitted RESPONSE rather than leaving it to the caller.

WHY THE RESPONSE IS PART OF THE CLASSIFICATION
----------------------------------------------
Knowing what went wrong is only half of it. §8 lists failure classes and expects different handling -
retry, re-observe, escalate, stop. Encoding the permitted response beside the class means a caller
cannot retry something that must be escalated, because `may_retry` is derived from the class rather
than decided ad hoc at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional


class FailureClass(str, Enum):
    """What went wrong, in terms that decide what to do about it."""

    #: The organisation has not supplied a fact or document. NOT a fault, and NOT retryable.
    MISSING_BUSINESS_INFORMATION = "MISSING_BUSINESS_INFORMATION"
    #: The page refused the data Granada supplied - the value is wrong or malformed.
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    #: Transient: the network, a timeout, a 502.
    TRANSIENT_TECHNICAL = "TRANSIENT_TECHNICAL"
    #: The page structure moved. Re-observe rather than re-run.
    LAYOUT_CHANGED = "LAYOUT_CHANGED"
    #: The session lapsed.
    SESSION_EXPIRED = "SESSION_EXPIRED"
    #: A CAPTCHA or equivalent. A human is required and Granada must not circumvent it.
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    #: The site refused access. An answer, not an obstacle.
    ACCESS_DENIED = "ACCESS_DENIED"
    #: A bound was reached.
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    #: A submission may or may not have landed. Reconciliation, never retry.
    UNCERTAIN_SUBMISSION = "UNCERTAIN_SUBMISSION"
    #: Not diagnosed, so not retried.
    UNKNOWN = "UNKNOWN"


class Response(str, Enum):
    """What may be done in response. A closed set, so an unexpected value is a bug."""

    RETRY_BOUNDED = "RETRY_BOUNDED"
    REOBSERVE = "REOBSERVE"
    REAUTHENTICATE = "REAUTHENTICATE"
    CORRECT_AND_RETRY = "CORRECT_AND_RETRY"
    AWAIT_ORGANISATION = "AWAIT_ORGANISATION"
    AWAIT_HUMAN = "AWAIT_HUMAN"
    RECONCILE = "RECONCILE"
    STOP = "STOP"


#: The permitted response for each class. A mapping rather than a method, so the policy is readable in
#: one place and a change to it is visible.
RESPONSES: dict[FailureClass, Response] = {
    # The organisation supplies it. Nothing Granada does will help, so the workflow must park rather
    # than be retried - this is the row that prevents the runaway loop the earlier directive fixed.
    FailureClass.MISSING_BUSINESS_INFORMATION: Response.AWAIT_ORGANISATION,
    FailureClass.VALIDATION_REJECTED: Response.CORRECT_AND_RETRY,
    FailureClass.TRANSIENT_TECHNICAL: Response.RETRY_BOUNDED,
    FailureClass.LAYOUT_CHANGED: Response.REOBSERVE,
    FailureClass.SESSION_EXPIRED: Response.REAUTHENTICATE,
    FailureClass.HUMAN_VERIFICATION_REQUIRED: Response.AWAIT_HUMAN,
    # §9: a 403 is an answer. Retrying it is ignoring what the site said.
    FailureClass.ACCESS_DENIED: Response.STOP,
    FailureClass.RESOURCE_LIMIT: Response.STOP,
    FailureClass.UNCERTAIN_SUBMISSION: Response.RECONCILE,
    FailureClass.UNKNOWN: Response.STOP,
}

#: Responses that mean "this will not resolve without something from outside Granada".
PARKING_RESPONSES: frozenset[Response] = frozenset(
    {Response.AWAIT_ORGANISATION, Response.AWAIT_HUMAN, Response.STOP, Response.RECONCILE}
)


@dataclass(frozen=True)
class Diagnosis:
    """What was determined, what may be done, and why.

    `needs_human` and `organisation_actionable` are separated because they are different questions:
    something can need a human who is not the organisation (a CAPTCHA), and the notification and
    workflow routing differ.
    """

    failure: FailureClass
    response: Response
    detail: str
    #: What the organisation specifically must supply, when this is their information to provide.
    missing: list[str] = field(default_factory=list)
    diagnosed_at: Optional[datetime] = None

    @property
    def retryable(self) -> bool:
        return self.response in (Response.RETRY_BOUNDED, Response.REOBSERVE, Response.CORRECT_AND_RETRY)

    @property
    def parks(self) -> bool:
        """Whether the workflow should stop scheduling and wait.

        The earlier directive's runaway-loop defect was workflows in WAITING being rescheduled with no
        new information. A parking diagnosis is what makes waiting terminal until something changes.
        """
        return self.response in PARKING_RESPONSES

    @property
    def is_organisations_to_fix(self) -> bool:
        """Whether this is missing business information - the case that must never be reported as an
        infrastructure error, and whose responsibility is the NGO's."""
        return self.failure is FailureClass.MISSING_BUSINESS_INFORMATION

    @property
    def organisation_actionable(self) -> bool:
        return self.response is Response.AWAIT_ORGANISATION


def _now() -> datetime:
    return datetime.now(timezone.utc)


def diagnose(
    *,
    failure: FailureClass,
    detail: str = "",
    missing: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> Diagnosis:
    """Classify a failure and derive the permitted response.

    `missing` is only meaningful for MISSING_BUSINESS_INFORMATION, and is deliberately part of the
    diagnosis rather than the message: "the package is blocked" is not actionable, "verified
    registration certificate missing" is. The earlier directive asked for precisely this.
    """
    if failure is FailureClass.MISSING_BUSINESS_INFORMATION and not missing:
        # A missing-information diagnosis with nothing named is not actionable, and an operator
        # cannot tell what to ask for. Refusing to produce it is better than producing a vague one.
        raise ValueError(
            "MISSING_BUSINESS_INFORMATION must name what is missing; an unnamed blocker is not "
            "actionable and would be indistinguishable from a technical failure"
        )
    return Diagnosis(
        failure=failure,
        response=RESPONSES[failure],
        detail=detail,
        missing=list(missing or []),
        diagnosed_at=now or _now(),
    )


def from_page_evidence(
    *,
    validation_messages: Optional[list[str]] = None,
    login_present: bool = False,
    challenge_present: bool = False,
    status_code: Optional[int] = None,
    timed_out: bool = False,
    layout_unrecognised: bool = False,
    missing_facts: Optional[list[str]] = None,
) -> Diagnosis:
    """Classify from what the page and the network actually showed.

    Ordered so the most specific and most consequential evidence wins. A challenge outranks a
    validation message, because a form that cannot be reached cannot be corrected.
    """
    if challenge_present:
        return diagnose(
            failure=FailureClass.HUMAN_VERIFICATION_REQUIRED,
            detail="the site requires human verification; Granada does not circumvent it",
        )
    if login_present:
        return diagnose(
            failure=FailureClass.SESSION_EXPIRED,
            detail="the session is no longer authenticated",
        )
    if status_code == 403:
        return diagnose(
            failure=FailureClass.ACCESS_DENIED,
            detail="the site refused access (403); this is an answer, not an obstacle",
        )
    if status_code is not None and 500 <= status_code < 600:
        return diagnose(
            failure=FailureClass.TRANSIENT_TECHNICAL,
            detail=f"the site returned {status_code}",
        )
    if timed_out:
        return diagnose(
            failure=FailureClass.TRANSIENT_TECHNICAL,
            detail="the request timed out",
        )
    if missing_facts:
        # Checked BEFORE validation messages: if Granada never had the value, the page rejecting it is
        # a symptom, and reporting the symptom would send the NGO to fix the wrong thing.
        return diagnose(
            failure=FailureClass.MISSING_BUSINESS_INFORMATION,
            detail="the page requires information Granada does not hold in verified form",
            missing=list(missing_facts),
        )
    if validation_messages:
        return diagnose(
            failure=FailureClass.VALIDATION_REJECTED,
            detail="; ".join(validation_messages[:3]),
        )
    if layout_unrecognised:
        return diagnose(
            failure=FailureClass.LAYOUT_CHANGED,
            detail="the page structure is not recognised; re-observe before acting",
        )
    return diagnose(failure=FailureClass.UNKNOWN, detail="the failure was not diagnosed")


@dataclass
class Attempt:
    """One recovery attempt, recorded so a retry that eventually worked can be told apart from one
    that never had a problem."""

    failure: FailureClass
    response: Response
    at: datetime
    detail: str = ""


class RecoveryController:
    """Bounded replanning for one run.

    Holds the attempt history so a bound is enforced across attempts rather than within one, and so a
    loop cannot form by alternating between two failure classes indefinitely.
    """

    def __init__(
        self,
        *,
        max_attempts_per_class: int = 3,
        max_total_attempts: int = 8,
        now: Optional[Any] = None,
    ) -> None:
        self.max_attempts_per_class = max_attempts_per_class
        self.max_total_attempts = max_total_attempts
        self._now = now or _now
        self.attempts: list[Attempt] = []

    def record(self, diagnosis: Diagnosis) -> None:
        self.attempts.append(
            Attempt(
                failure=diagnosis.failure,
                response=diagnosis.response,
                at=self._now(),
                detail=diagnosis.detail,
            )
        )

    def may_attempt(self, diagnosis: Diagnosis) -> tuple[bool, str]:
        """Whether the diagnosed response may be taken, given the history.

        A parking diagnosis is refused here as well as flagged, because a caller that ignores `parks`
        should still be stopped.
        """
        if not diagnosis.retryable:
            return False, (
                f"{diagnosis.failure.value} is answered with {diagnosis.response.value}, which is not "
                "a retry"
            )
        same = sum(1 for a in self.attempts if a.failure is diagnosis.failure)
        if same >= self.max_attempts_per_class:
            return False, (
                f"{diagnosis.failure.value} has already been attempted {same} time(s); the bound is "
                f"{self.max_attempts_per_class}"
            )
        if len(self.attempts) >= self.max_total_attempts:
            return False, (
                f"the run has made {len(self.attempts)} recovery attempts; the bound is "
                f"{self.max_total_attempts}"
            )
        return True, "within bounds"

    def loop_detected(self) -> bool:
        """Whether the same failure has recurred without changing.

        Distinct from a bound being reached: two alternating classes can stay under every per-class
        bound while making no progress at all.
        """
        if len(self.attempts) < 4:
            return False
        recent = [a.failure for a in self.attempts[-4:]]
        return len(set(recent)) <= 2 and recent[0] == recent[2] and recent[1] == recent[3]

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": len(self.attempts),
            "by_failure": {
                f.value: sum(1 for a in self.attempts if a.failure is f)
                for f in {a.failure for a in self.attempts}
            },
            "loop_detected": self.loop_detected(),
            "history": [
                {"failure": a.failure.value, "response": a.response.value, "at": a.at.isoformat()}
                for a in self.attempts
            ],
        }


def describe() -> dict[str, Any]:
    """The rules, stated where a reviewer will find them."""
    return {
        "classes": sorted(c.value for c in FailureClass),
        "responses": {c.value: RESPONSES[c].value for c in FailureClass},
        "the_distinction": (
            "a missing FACT is the organisation's to supply, parks the workflow and is never retried; "
            "a TECHNICAL failure is Granada's to fix and is not escalated to the NGO"
        ),
        "parking_responses": sorted(r.value for r in PARKING_RESPONSES),
        "never_retried": [
            FailureClass.MISSING_BUSINESS_INFORMATION.value,
            FailureClass.HUMAN_VERIFICATION_REQUIRED.value,
            FailureClass.ACCESS_DENIED.value,
            FailureClass.UNCERTAIN_SUBMISSION.value,
            FailureClass.UNKNOWN.value,
        ],
        "does_not_do": [
            "it does not perform actions - browser_runtime does",
            "it does not reconcile an uncertain submission - submission_lifecycle does",
        ],
    }
