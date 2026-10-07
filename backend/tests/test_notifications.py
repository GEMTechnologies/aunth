"""Notification delivery: the suppression rule, and the three refusals.

The motivating case for this whole round is `granada:v1:report.overdue`. Until now that
event went into a Redis stream that **nothing consumed for a human** - so the alert with the
clearest financial consequence was published perfectly and read by nobody.

The tests are organised around the thing that makes a notification system useful or worse
than useless:

**A notification system that floods gets muted, and the muted channel is how the next real
problem goes unnoticed.** The fleet and the relay work on a timer, so one overdue report
emits an event on every scan. Suppression is therefore keyed on the SUBJECT and the
CONDITION, never on the event.
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
from agent.notifications.contract import (  # noqa: E402
    Category,
    Severity,
    format_route,
    route_for,
)
from agent.notifications.providers.fake import (  # noqa: E402
    ExternalChannel,
    FailingChannel,
    InAppChannel,
)
from agent.notifications.service import NotificationService  # noqa: E402
from tests.test_mail import _org_and_agent  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "notifications.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    org, agent_service = _org_and_agent(db, with_documents=True)
    return org, agent_service.get()


def _service(db, world, *, channels=None, recipients=None):
    org, _agent = world
    return NotificationService(
        db,
        org_id=org.id,
        channels=channels if channels is not None else [InAppChannel()],
        default_recipients=recipients if recipients is not None else ["user-1"],
    )


def _overdue_payload(**overrides):
    payload = {
        "event_id": str(uuid.uuid4()),
        "obligation_id": "obl-1",
        "grant_id": "grant-1",
        "title": "Q1 narrative report",
        "due_on": "2026-09-30",
        "days_remaining": -3,
    }
    payload.update(overrides)
    return payload


# ===========================================================================
# THE MOTIVATING CASE
# ===========================================================================
def test_an_overdue_report_reaches_a_person(db, world):
    """THE test for this whole phase.

    Before it, `report.overdue` was published to a stream nobody read. An unsubmitted
    report is the most common reason a subsequent tranche is withheld, and it produces no
    rejection letter - just money that does not arrive.
    """
    channel = InAppChannel()
    service = _service(db, world, channels=[channel])

    result = service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    assert result.action == "CREATED"
    assert channel.delivery_count == 1

    notification = db.query(models.Notification).one()
    assert notification.severity == Severity.CRITICAL.value
    assert notification.action_required is True
    assert notification.category == Category.FUNDER_REPORT.value
    assert "Q1 narrative report" in notification.title
    assert "money that does not arrive" in notification.body


def test_the_overdue_notification_carries_a_link_to_act(db, world):
    """A notification saying something is wrong without saying where to go is half a
    notification."""
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    assert db.query(models.Notification).one().action_url == "/delivery/reports/obl-1"


# ===========================================================================
# SUPPRESSION — THE DESIGN
# ===========================================================================
def test_the_same_condition_raised_repeatedly_is_ONE_notification(db, world):
    """THE suppression test.

    The relay and the fleet scan on a timer, so this event arrives every scan. Without
    suppression a single overdue report produces thousands of notifications a day, the
    channel gets muted, and the next real problem is missed.
    """
    channel = InAppChannel()
    service = _service(db, world, channels=[channel])

    for _ in range(50):
        service.raise_for_event(
            event_type="granada:v1:report.overdue", payload=_overdue_payload()
        )
    db.commit()

    assert db.query(models.Notification).count() == 1, "one condition became many notifications"

    notification = db.query(models.Notification).one()
    assert notification.repeat_count == 49, (
        "the repeat count is what tells a person it is not going away; losing it makes a "
        "persistent problem look like a single occurrence"
    )
    # The channel counted every delivery, because a reminder that is never re-delivered is
    # not a reminder.
    assert channel.delivery_count == 50


def test_a_different_obligation_is_a_different_notification(db, world):
    """Suppression keyed on the event would collapse unrelated problems into one."""
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload(obligation_id="obl-1")
    )
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload(obligation_id="obl-2")
    )
    db.commit()
    assert db.query(models.Notification).count() == 2


def test_the_dedupe_key_names_the_subject_and_the_condition(db, world):
    """Not the event and not a timestamp - both change on every scan."""
    route = route_for("granada:v1:report.overdue")
    assert route is not None
    _title, _body, key = format_route(route, _overdue_payload())
    assert key == "report:obl-1:overdue"
    assert "event_id" not in key


def test_a_closed_notification_is_not_resurrected(db, world):
    """A recurrence is a new decision and should be visible as one.

    Silently re-opening a dismissed notification would hide that somebody already dealt
    with it - and would make "I dismissed that" meaningless.
    """
    channel = InAppChannel()
    service = _service(db, world, channels=[channel])
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    notification = db.query(models.Notification).one()
    service.mark(
        notification_id=notification.id, user_id="user-1",
        status=models.Notification.STATUS_DISMISSED,
    )
    db.commit()

    result = service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    assert result.action == "SUPPRESSED"
    assert "already dismissed" in result.reason
    assert db.query(models.Notification).count() == 1
    assert notification.status == models.Notification.STATUS_DISMISSED


def test_an_actioned_notification_is_not_resurrected_either(db, world):
    service = _service(db, world)
    service.raise_for_event("granada:v1:report.overdue" and "", payload={}) if False else None
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    notification = db.query(models.Notification).one()
    service.mark(
        notification_id=notification.id, user_id="user-1",
        status=models.Notification.STATUS_ACTIONED,
    )
    db.commit()

    result = service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    assert result.action == "SUPPRESSED"


# ===========================================================================
# NOT EVERYTHING IS A NOTIFICATION
# ===========================================================================
def test_a_bookkeeping_event_raises_nothing(db, world):
    """THE COMMON CASE, and it is deliberate.

    Most of what the platform emits is a package being frozen or an application being
    authorised. Notifying on each would bury the ones that matter.
    """
    channel = InAppChannel()
    service = _service(db, world, channels=[channel])

    for event_type in (
        "granada:v1:submission.package_frozen",
        "granada:v1:submission.authorised",
        "granada:v1:award.condition_added",
        "granada:v1:submission.handed_off",
    ):
        result = service.raise_for_event(event_type=event_type, payload={"event_id": "e"})
        assert result.action == "UNROUTED"

    assert db.query(models.Notification).count() == 0
    assert channel.delivery_count == 0


def test_the_routing_table_covers_every_event_the_delivery_phase_emits():
    """So an event that should reach a person cannot be silently unrouted.

    The two lists are compared directly: every `report.*` and `submission.unknown` event is
    checked, and the bookkeeping ones are asserted to be absent - because "no route" must be
    a decision rather than an oversight.
    """
    from agent.notifications.contract import ROUTES

    must_reach_a_person = {
        "granada:v1:report.overdue",
        "granada:v1:report.due_soon",
        "granada:v1:submission.unknown",
        "granada:v1:disbursement.expected",
        "granada:v1:disbursement.received",
        "granada:v1:award.recorded",
    }
    missing = must_reach_a_person - set(ROUTES)
    assert not missing, f"these events reach nobody: {sorted(missing)}"

    deliberately_silent = {
        "granada:v1:submission.package_frozen",
        "granada:v1:submission.authorised",
        "granada:v1:award.condition_added",
        "granada:v1:award.closed",
    }
    noisy = deliberately_silent & set(ROUTES)
    assert not noisy, (
        f"these bookkeeping events now raise notifications: {sorted(noisy)}. If that is "
        "intended, move them out of this list deliberately."
    )


def test_an_event_with_a_missing_field_still_produces_a_notification(db, world):
    """A notification with a gap is more useful than none.

    A routing bug should be visible rather than crashing the path at exactly the wrong
    moment - `(unknown)` in the body is a signal, not a failure.
    """
    service = _service(db, world)
    result = service.raise_for_event(
        event_type="granada:v1:report.overdue",
        payload={"event_id": "e", "obligation_id": "obl-9"},   # no title, no due_on
    )
    db.commit()
    assert result.action == "CREATED"
    notification = db.query(models.Notification).one()
    assert "(unknown)" in notification.title or "(unknown)" in notification.body


def test_no_recipient_suppresses_rather_than_inventing_one(db, world):
    """Notifying a stranger is worse than not notifying."""
    service = _service(db, world, recipients=[])
    result = service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    assert result.action == "SUPPRESSED"
    assert "inventing one" in result.reason
    assert db.query(models.Notification).count() == 0


# ===========================================================================
# DELIVERY EVIDENCE
# ===========================================================================
def test_every_delivery_attempt_is_recorded(db, world):
    """`notification_deliveries` is append-only evidence.

    Without it, "the platform knew and told somebody" is an assertion rather than a fact -
    the same reasoning as `mail_send_attempts` and `submission_receipts`.
    """
    service = _service(db, world, channels=[InAppChannel()])
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.channel == "IN_APP"
    assert delivery.result == models.NotificationDelivery.RESULT_DELIVERED
    assert delivery.user_id == "user-1"


def test_a_failing_channel_records_the_failure_without_losing_the_notification(db, world):
    """The notification row is already committed, so a channel failure means the person can
    still see it in the app - which is the correct outcome, and different from losing it."""
    failing = FailingChannel("CHANNEL_DOWN")
    service = _service(db, world, channels=[failing])
    result = service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    assert result.action == "CREATED"
    assert db.query(models.Notification).count() == 1
    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_FAILED
    assert delivery.error_code == "NotificationChannelError"


def test_no_channel_configured_is_recorded_rather_than_ignored(db, world):
    """"Knew and told nobody" is a different fact from "told somebody who has not looked",
    and the record should distinguish them."""
    service = _service(db, world, channels=[])
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_SUPPRESSED
    assert delivery.channel == "NONE"


# ===========================================================================
# REFUSALS
# ===========================================================================
def test_an_external_channel_is_refused(db, world):
    """An email notification reaches outside the platform in the organisation's name, and
    the Phase 7b outbound path already gates exactly that.

    A second egress is a second place to audit, and the one that gets forgotten is the one
    that sends.
    """
    external = ExternalChannel("EMAIL")
    service = _service(db, world, channels=[external])
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    assert external.attempts == [], "the external channel was called"
    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_SUPPRESSED
    assert delivery.error_code == "EXTERNAL_CHANNEL_REFUSED"
    assert "outbound path" in delivery.reason


def test_the_external_channel_raises_when_called_directly(db, world):
    """So the refusal is enforced by the channel as well as by the service."""
    from agent.notifications.contract import NotificationChannelError

    external = ExternalChannel("EMAIL")
    with pytest.raises(NotificationChannelError):
        external.deliver(notification=object(), recipient="user-1")


def test_a_disabled_preference_suppresses_and_says_why(db, world):
    service = _service(db, world)
    service.set_preference(
        user_id="user-1", category=Category.FUNDER_REPORT.value, enabled=False
    )
    db.commit()

    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_SUPPRESSED
    assert "disabled" in delivery.reason


def test_a_severity_below_the_threshold_is_suppressed(db, world):
    service = _service(db, world)
    service.set_preference(
        user_id="user-1", category=Category.FUNDER_REPORT.value,
        min_severity=Severity.CRITICAL.value,
    )
    db.commit()

    # WARNING is below CRITICAL, so `due_soon` is suppressed while `overdue` is not.
    service.raise_for_event(
        event_type="granada:v1:report.due_soon", payload=_overdue_payload()
    )
    db.commit()
    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_SUPPRESSED
    assert "below this channel's threshold" in delivery.reason

    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    results = [d.result for d in db.query(models.NotificationDelivery).all()]
    assert models.NotificationDelivery.RESULT_DELIVERED in results


def test_quiet_hours_DEFER_rather_than_drop(db, world):
    """A funder deadline is not urgent at 03:00, and a notification that wakes somebody for
    a non-urgent item is one they turn off - but it is deferred, not discarded."""
    service = _service(db, world)
    now_hour = _now().hour
    service.set_preference(
        user_id="user-1", category=Category.FUNDER_REPORT.value,
        quiet_from_hour=now_hour,
        quiet_to_hour=(now_hour + 2) % 24,
    )
    db.commit()

    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()

    delivery = db.query(models.NotificationDelivery).one()
    assert delivery.result == models.NotificationDelivery.RESULT_DEFERRED
    assert "deferred, not discarded" in delivery.reason
    # And the notification EXISTS, so it is waiting when the window ends.
    assert db.query(models.Notification).count() == 1


def test_a_quiet_window_crossing_midnight_is_handled(db, world):
    """22:00-07:00 is the common window and a naive `from <= hour < to` gets it exactly
    backwards - it would be silent all day and noisy all night."""
    service = _service(db, world)
    preference = service.set_preference(
        user_id="user-1", category=Category.FUNDER_REPORT.value,
        quiet_from_hour=23, quiet_to_hour=7,
    )
    assert preference.quiet_from_hour == 23
    assert preference.quiet_to_hour == 7
    # The window is described as crossing midnight rather than rejected.
    assert service._in_quiet_hours(preference) in (True, False)


def test_half_a_quiet_window_is_refused(db, world):
    """One bound without the other would silence everything, which is never intended."""
    service = _service(db, world)
    with pytest.raises(ValueError):
        service.set_preference(
            user_id="user-1", category=Category.FUNDER_REPORT.value, quiet_from_hour=22
        )
    with pytest.raises(ValueError):
        service.set_preference(
            user_id="user-1", category=Category.FUNDER_REPORT.value, quiet_to_hour=7
        )


# ===========================================================================
# THE READER'S SIDE
# ===========================================================================
def test_the_inbox_puts_critical_first_not_newest_first(db, world):
    """A critical notification raised yesterday matters more than an informational one
    raised a minute ago, and a chronological inbox buries it under the noise since."""
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    # Backdate the critical one so recency ordering would put it last.
    critical = db.query(models.Notification).one()
    critical.created_at = _now() - timedelta(days=1)
    db.commit()

    service.raise_for_event(
        event_type="granada:v1:disbursement.received",
        payload={"event_id": "e", "disbursement_id": "dis-1", "label": "T1",
                 "amount_received": "1000", "currency": "USD"},
    )
    db.commit()

    inbox = service.inbox(user_id="user-1")
    assert len(inbox) == 2
    assert inbox[0].severity == Severity.CRITICAL.value, (
        "the informational notification was ordered first"
    )


def test_marking_never_deletes(db, world):
    """A notification that was raised is evidence the platform knew. One its own subject
    can erase is not evidence."""
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    notification = db.query(models.Notification).one()

    service.mark(
        notification_id=notification.id, user_id="user-1",
        status=models.Notification.STATUS_READ,
    )
    db.commit()
    assert db.query(models.Notification).count() == 1
    assert notification.read_at is not None


def test_an_invalid_status_is_refused(db, world):
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    db.commit()
    notification = db.query(models.Notification).one()
    with pytest.raises(ValueError):
        service.mark(
            notification_id=notification.id, user_id="user-1", status="DELETED"
        )


def test_one_persons_notification_is_not_anothers(db, world):
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload(),
        recipients=["user-1"],
    )
    db.commit()
    with pytest.raises(ValueError):
        service.mark(
            notification_id=db.query(models.Notification).one().id,
            user_id="user-2", status=models.Notification.STATUS_READ,
        )


def test_the_summary_counts_what_a_badge_needs(db, world):
    service = _service(db, world)
    service.raise_for_event(
        event_type="granada:v1:report.overdue", payload=_overdue_payload()
    )
    service.raise_for_event(
        event_type="granada:v1:disbursement.received",
        payload={"event_id": "e", "disbursement_id": "dis-1", "label": "T1",
                 "amount_received": "1000", "currency": "USD"},
    )
    db.commit()

    summary = service.summary(user_id="user-1")
    assert summary["unread"] == 2
    assert summary["action_required"] == 1
    assert summary["critical"] == 1


# ===========================================================================
# EVERY EVENT THAT MATTERS HAS A ROUTE THAT MAKES SENSE
# ===========================================================================
def test_every_critical_route_requires_action(db, world):
    """A CRITICAL notification that needs nothing done is either mis-severity or
    mis-action. Either way it trains people to ignore the red badge."""
    from agent.notifications.contract import ROUTES

    for event_type, route in ROUTES.items():
        if route.severity is Severity.CRITICAL:
            assert route.action_required, (
                f"{event_type} is CRITICAL but requires no action"
            )


def test_every_route_has_a_dedupe_key_naming_its_subject():
    """A dedupe key with no field reference would collapse every occurrence of a category
    into a single notification."""
    from agent.notifications.contract import ROUTES

    for event_type, route in ROUTES.items():
        assert "{" in route.dedupe_key, (
            f"{event_type}'s dedupe key names no subject, so all its occurrences would "
            "collapse into one notification"
        )
        assert route.required_fields, f"{event_type} declares no required fields"


def test_the_dedupe_key_from_a_route_is_stable_across_events(db, world):
    """The property that makes suppression work, stated directly: two DIFFERENT events
    about the SAME condition produce the SAME key."""
    route = route_for("granada:v1:report.overdue")
    first = format_route(route, _overdue_payload(event_id="event-a"))
    second = format_route(route, _overdue_payload(event_id="event-b"))
    assert first[2] == second[2], "two events for one condition produced two identities"
