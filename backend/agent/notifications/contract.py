"""What a notification is, and where it may go.

The routing table is the substance of this module
-------------------------------------------------
It maps an **event** to a **decision**: how urgent, who needs it, whether it needs action,
and - the important one - **what makes two occurrences the same notification**.

The dedupe key is not bookkeeping. The fleet and the relay scan on a timer, so a single
overdue report produces an event every scan. Without suppression that is thousands of
notifications a day for one problem, and the channel gets muted - which is exactly how a
real problem goes unnoticed. **A notification system that floods is worse than none**,
because it trains people to ignore the channel that would have told them.

Notifications are not actions
-----------------------------
An in-app notification is internal: it writes a row and nothing leaves the platform. An
**email** notification leaves, so it reuses the Phase 7b gated outbound path rather than
inventing a second egress - one gate, one place to audit. The service enforces that; this
module only describes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class Severity(str, Enum):
    """How much this matters.

    Only three, deliberately. A severity scale with more gradations gets argued about and
    then mapped back to three by every consumer anyway.
    """

    #: Money or an application is at risk, right now.
    CRITICAL = "CRITICAL"
    #: Something needs attention.
    WARNING = "WARNING"
    #: Worth knowing.
    INFO = "INFO"

    @property
    def rank(self) -> int:
        return {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[self]

    def at_least(self, other: "Severity") -> bool:
        """Whether this meets a minimum threshold, for preference filtering."""
        return self.rank >= other.rank


class Category(str, Enum):
    """What the notification is about, so preferences can be per-topic."""

    #: A funder report approaching or past its deadline.
    FUNDER_REPORT = "FUNDER_REPORT"
    #: A condition blocking a disbursement.
    PAYMENT_BLOCKED = "PAYMENT_BLOCKED"
    #: A tranche expected but not received.
    DISBURSEMENT_LATE = "DISBURSEMENT_LATE"
    #: An application whose outcome is unknown.
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    #: Correspondence awaiting a person.
    MAIL_APPROVAL = "MAIL_APPROVAL"
    #: An outbound email whose outcome is unknown.
    MAIL_UNCERTAIN = "MAIL_UNCERTAIN"
    #: The pipeline itself is not working.
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    #: An award or a submitted report.
    DELIVERY_PROGRESS = "DELIVERY_PROGRESS"


@dataclass(frozen=True)
class Route:
    """The decision for one event type."""

    category: Category
    severity: Severity
    title: str
    body: str
    action_required: bool
    #: The template for the suppression identity. Formatted with the event payload.
    #:
    #: It names the SUBJECT and the CONDITION - `report:{obligation_id}:overdue` - never
    #: the event or its timestamp, because those differ on every scan and would make every
    #: occurrence a new notification.
    dedupe_key: str
    action_url: Optional[str] = None
    #: Fields the body and dedupe key may use. A missing one is a routing bug rather than
    #: a crash, and the service reports it.
    required_fields: tuple[str, ...] = ()


#: Event type -> how it is delivered. Every event the delivery and mail phases emit, plus
#: the health signals from the metrics work.
ROUTES: dict[str, Route] = {
    # -- money, in order of consequence ------------------------------------
    "granada:v1:report.overdue": Route(
        category=Category.FUNDER_REPORT,
        severity=Severity.CRITICAL,
        title="Funder report overdue: {title}",
        body=(
            "The report '{title}' was due on {due_on}. An unsubmitted report is the most "
            "common reason a subsequent tranche is withheld, and it produces no rejection "
            "letter - just money that does not arrive."
        ),
        action_required=True,
        # One notification per OBLIGATION. Raising it again is a repeat, not a new item.
        dedupe_key="report:{obligation_id}:overdue",
        action_url="/delivery/reports/{obligation_id}",
        required_fields=("obligation_id", "title"),
    ),
    "granada:v1:report.due_soon": Route(
        category=Category.FUNDER_REPORT,
        severity=Severity.WARNING,
        title="Funder report due soon: {title}",
        body="The report '{title}' is due on {due_on} ({days_remaining} days).",
        action_required=True,
        dedupe_key="report:{obligation_id}:due_soon",
        action_url="/delivery/reports/{obligation_id}",
        required_fields=("obligation_id", "title"),
    ),
    "granada:v1:submission.unknown": Route(
        category=Category.SUBMISSION_UNCERTAIN,
        severity=Severity.CRITICAL,
        title="Application outcome unknown",
        body=(
            "Granada does not know whether the funder received this application, so it "
            "will not file again - a second application can disqualify both. "
            "Reconciliation is the only safe next step."
        ),
        action_required=True,
        dedupe_key="submission:{submission_package_id}:unknown",
        action_url="/submissions/{submission_package_id}",
        required_fields=("submission_package_id",),
    ),
    "granada:v1:disbursement.expected": Route(
        category=Category.DISBURSEMENT_LATE,
        severity=Severity.INFO,
        title="Tranche expected: {label}",
        body="{amount} {currency} is expected on {expected_on}.",
        action_required=False,
        dedupe_key="disbursement:{disbursement_id}:expected",
        required_fields=("disbursement_id",),
    ),
    "granada:v1:disbursement.received": Route(
        category=Category.DELIVERY_PROGRESS,
        severity=Severity.INFO,
        title="Tranche received: {label}",
        body="{amount_received} {currency} received.",
        action_required=False,
        dedupe_key="disbursement:{disbursement_id}:received",
        required_fields=("disbursement_id",),
    ),
    "granada:v1:award.condition_met": Route(
        category=Category.PAYMENT_BLOCKED,
        severity=Severity.INFO,
        title="Condition satisfied",
        body="A condition on grant {grant_id} has been met with evidence.",
        action_required=False,
        dedupe_key="condition:{condition_id}:met",
        required_fields=("condition_id",),
    ),
    "granada:v1:award.recorded": Route(
        category=Category.DELIVERY_PROGRESS,
        severity=Severity.INFO,
        title="Award recorded: {reference}",
        body="{awarded_amount} {currency} awarded.",
        action_required=False,
        dedupe_key="award:{grant_id}:recorded",
        required_fields=("grant_id",),
    ),
    "granada:v1:report.submitted": Route(
        category=Category.DELIVERY_PROGRESS,
        severity=Severity.INFO,
        title="Report submitted",
        body="A report went to the funder with reference {reference}.",
        action_required=False,
        dedupe_key="report:{obligation_id}:submitted",
        required_fields=("obligation_id",),
    ),
}


def route_for(event_type: str) -> Optional[Route]:
    """The route for an event type, or None when nothing should be raised.

    **None is the common case and it is deliberate.** Most events are bookkeeping - a
    package frozen, an application authorised - and raising a notification for each would
    bury the ones that matter. Nothing is raised unless it is in the table above.
    """
    return ROUTES.get(event_type)


def format_route(route: Route, payload: dict[str, Any]) -> tuple[str, str, str]:
    """Render the title, body and dedupe key.

    Missing fields are rendered as ``(unknown)`` rather than raising. A notification with a
    gap is more useful than no notification, and a notification path that crashes on an
    unexpected payload is a path that fails silently at exactly the wrong moment.
    """
    safe = _MissingTolerant(payload)
    return (
        route.title.format_map(safe)[:500],
        route.body.format_map(safe),
        route.dedupe_key.format_map(safe)[:255],
    )


class _MissingTolerant(dict):
    """A mapping that yields a placeholder instead of raising KeyError."""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "(unknown)"


@dataclass
class DeliveryOutcome:
    """What one channel did."""

    channel: str
    result: str
    reason: Optional[str] = None
    provider_reference: Optional[str] = None
    error_code: Optional[str] = None


@dataclass
class RaiseResult:
    """What raising a notification decided."""

    notification_id: Optional[str]
    action: str            # CREATED | REPEATED | SUPPRESSED | UNROUTED
    reason: str = ""
    deliveries: list[DeliveryOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "action": self.action,
            "reason": self.reason,
            "deliveries": [
                {
                    "channel": d.channel, "result": d.result, "reason": d.reason,
                    "provider_reference": d.provider_reference, "error_code": d.error_code,
                }
                for d in self.deliveries
            ],
        }


class NotificationChannelError(RuntimeError):
    """A channel-side failure the caller must handle rather than retry blindly."""


@runtime_checkable
class NotificationChannel(Protocol):
    """Where a notification goes.

    ``INTERNAL`` is the guarantee that matters. A channel that reports itself internal
    writes inside the platform and needs no gate. One that does not - email, a webhook, a
    chat message - is an **external action**, and the service refuses to use it except
    through the gated outbound path.
    """

    name: str
    #: True when delivery stays inside the platform.
    internal: bool

    def deliver(self, *, notification: Any, recipient: str) -> DeliveryOutcome:
        ...
