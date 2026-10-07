"""The end of the path: an outbox event becomes a notification a person can see.

This is the integration test for the round. Every piece of the notification path already
exists and is tested in isolation - routing, suppression, delivery evidence. What this
checks is that the pieces are **connected**, because the defect this round fixes is exactly
a missing connection: every event reached Redis and nothing consumed it for a human.

It also covers the failure that an integration test exists to catch and a unit test cannot:
`deliver_event(self.session, ...)` where the attribute is `self.db`. That compiles cleanly
and raises at the first routed event.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.notifications.integration import deliver_event, recipients_for  # noqa: E402
from events.relay import OutboxRelay  # noqa: E402
from tests.test_mail import _org_and_agent  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


class RecordingPublisher:
    """Accepts everything, and records it."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish_raw(self, *, stream: str, fields: dict) -> None:
        self.published.append({"stream": stream, "fields": fields})


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "notify_integration.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    org, agent_service = _org_and_agent(db, with_documents=True)
    return org, agent_service.get()


def _event(db, org, event_type: str, payload: dict) -> models.OutboxEvent:
    event = models.OutboxEvent(
        id=str(uuid.uuid4()),
        org_id=org.id,
        stream=f"granada:v1:{event_type.split(':')[-1].split('.')[0]}",
        event_type=event_type,
        payload=payload,
        created_at=_now(),
        attempts=0,
    )
    db.add(event)
    db.commit()
    return event


# ===========================================================================
# THE MISSING LINK
# ===========================================================================
def test_an_outbox_event_becomes_a_notification_through_the_relay(db, world):
    """THE integration test.

    Before this, the relay published `report.overdue` to Redis and stopped. The event was
    durable, the relay was healthy, the dashboard was green - and nobody was told.
    """
    org, _agent = world
    _event(db, org, "granada:v1:report.overdue", {
        "obligation_id": "obl-1", "grant_id": "grant-1",
        "title": "Q1 narrative", "due_on": "2026-09-30",
    })

    relay = OutboxRelay(db, RecordingPublisher())
    published = relay.drain_once()
    db.commit()

    assert published == 1
    assert relay.notified == 1, "the event was published but became no notification"
    assert relay.notify_failures == 0

    notification = db.query(models.Notification).one()
    assert notification.org_id == org.id
    assert notification.severity == models.Notification.SEVERITY_CRITICAL
    assert notification.action_required is True


def test_a_bookkeeping_event_publishes_without_notifying(db, world):
    """The relay must still drain everything. Routing decides what reaches a person, and
    most events are bookkeeping."""
    org, _agent = world
    _event(db, org, "granada:v1:submission.package_frozen", {"submission_package_id": "p-1"})

    relay = OutboxRelay(db, RecordingPublisher())
    assert relay.drain_once() == 1
    db.commit()

    assert relay.notified == 0
    assert relay.notify_failures == 0
    assert db.query(models.Notification).count() == 0


def test_a_notification_failure_does_not_lose_the_event(db, world, monkeypatch):
    """The event HAS reached the stream. Converting a delivered event into a retried one
    because the notifier threw would duplicate it downstream."""
    org, _agent = world
    event = _event(db, org, "granada:v1:report.overdue", {
        "obligation_id": "obl-1", "title": "Q1", "due_on": "2026-09-30",
    })

    import agent.notifications.integration as integration

    def explode(*args, **kwargs):
        raise RuntimeError("the notifier is down")

    monkeypatch.setattr(integration, "deliver_event", explode)

    relay = OutboxRelay(db, RecordingPublisher())
    assert relay.drain_once() == 1
    db.commit()

    assert relay.notify_failures == 1
    assert relay.notified == 0
    # THE POINT: the event is published and marked, not retried.
    db.refresh(event)
    assert event.published_at is not None, "a delivered event was left unpublished"
    assert event.attempts == 1


def test_the_recipient_is_discovered_not_configured(db, world):
    """A new organisation gets notified about its own overdue report without anybody
    setting anything up - otherwise the default state is silence."""
    org, _agent = world
    recipients = recipients_for(db, org.id)
    assert org.owner_user_id in recipients


def test_an_explicit_preference_adds_a_recipient(db, world):
    """So a person who has opted in is not missed for not being the owner."""
    org, _agent = world
    db.add(
        models.NotificationPreference(
            id=str(uuid.uuid4()), org_id=org.id, user_id="extra-user",
            category="FUNDER_REPORT", channel="IN_APP", enabled=True,
            created_at=_now(),
        )
    )
    db.commit()
    recipients = recipients_for(db, org.id)
    assert "extra-user" in recipients
    assert org.owner_user_id in recipients


def test_a_disabled_preference_does_not_add_a_recipient(db, world):
    org, _agent = world
    db.add(
        models.NotificationPreference(
            id=str(uuid.uuid4()), org_id=org.id, user_id="opted-out",
            category="FUNDER_REPORT", channel="IN_APP", enabled=False,
            created_at=_now(),
        )
    )
    db.commit()
    assert "opted-out" not in recipients_for(db, org.id)


def test_deliver_event_returns_none_for_an_unrouted_event(db, world):
    """None is the signal for "deliberately silent", so it must be distinguishable from a
    failure."""
    org, _agent = world
    event = _event(db, org, "granada:v1:award.condition_added", {"condition_id": "c-1"})
    assert deliver_event(db, event=event) is None


def test_a_cross_tenant_event_with_no_org_is_skipped(db, world):
    """No organisation means no audience. Guessed at, it would notify a stranger."""
    event = models.OutboxEvent(
        id=str(uuid.uuid4()), org_id=None, stream="s",
        event_type="granada:v1:report.overdue",
        payload={"obligation_id": "o", "title": "t", "due_on": "d"},
        created_at=_now(), attempts=0,
    )
    db.add(event)
    db.commit()
    assert deliver_event(db, event=event) is None


def test_the_default_channel_is_internal_only():
    """So the default cannot become an ungated egress. An email notification leaves the
    platform in the organisation's name, and the outbound path already gates that."""
    from agent.notifications.integration import _default_channels

    for channel in _default_channels():
        assert getattr(channel, "internal", False) is True, (
            f"{channel.name} reports itself external; the default must not be able to send "
            "outside the platform"
        )


def test_routing_the_same_event_twice_through_the_relay_is_still_one_notification(db, world):
    """Suppression holds through the integration, which is where the timer-driven flood
    would actually happen."""
    org, _agent = world
    for _ in range(10):
        _event(db, org, "granada:v1:report.overdue", {
            "obligation_id": "obl-1", "title": "Q1", "due_on": "2026-09-30",
        })

    relay = OutboxRelay(db, RecordingPublisher())
    published = relay.drain_once()
    db.commit()

    assert published == 10, "every event should still be published"
    assert db.query(models.Notification).count() == 1, (
        "ten scans of one overdue condition produced more than one notification"
    )
