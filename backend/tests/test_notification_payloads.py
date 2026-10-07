"""Every routed event must carry the fields its route declares.

WHY THIS TEST EXISTS
--------------------
`contract.py` declares `required_fields` per event; the emitters in `delivery/service.py`
build the payloads. They are in different files, and nothing checked one against the other.

So `report.overdue` declared `("obligation_id", "title")` and emitted no `title`. The
notification rendered:

    Funder report overdue: (unknown)

Which tells a person that *something* is overdue and not which thing. It went unnoticed
precisely because the missing-field path is designed to degrade rather than crash - a
degraded notification is silent, and a crash would not be.

This runs the REAL services and inspects the events they actually emit, so a new emitter, a
renamed field or a changed route is caught by running the pipeline rather than by reading it.
"""

from __future__ import annotations

import re
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
from agent.mail import ceiling  # noqa: E402
from agent.notifications.contract import ROUTES, route_for  # noqa: E402
from tests.test_delivery import _handover, _service, _submitted_package  # noqa: E402
from tests.test_mail import _opportunity_and_application, _org_and_agent  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "payloads.db")
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


# ===========================================================================
# THE STRUCTURAL CHECK
# ===========================================================================
def test_every_route_declares_a_dedupe_key_and_required_fields():
    """So a route cannot be added without saying what identifies it and what it needs."""
    for event_type, route in ROUTES.items():
        assert route.required_fields, f"{event_type} declares no required fields"
        assert "{" in route.dedupe_key, f"{event_type} has a constant dedupe key"


def test_every_required_field_appears_in_at_least_one_template():
    """A required field nobody uses is a field nobody will supply.

    Either the body needs it - in which case declaring it is right - or the declaration is
    stale and should go, because a requirement that is never consumed is a requirement that
    silently goes unenforced.
    """
    for event_type, route in ROUTES.items():
        template = route.title + route.body + route.dedupe_key + (route.action_url or "")
        for field in route.required_fields:
            assert f"{{{field}}}" in template or f"{{{field}}}" in (
                route.action_url or ""
            ), f"{event_type} requires {field!r} but no template uses it"


# ===========================================================================
# THE BEHAVIOURAL CHECK: RUN THE EMITTERS AND LOOK AT WHAT THEY EMIT
# ===========================================================================
def _emitted(db, org_id: str) -> list[models.OutboxEvent]:
    return db.query(models.OutboxEvent).filter(
        models.OutboxEvent.org_id == org_id
    ).all()


def _assert_payloads_satisfy_routes(events: list[models.OutboxEvent]) -> list[str]:
    problems: list[str] = []
    for event in events:
        route = route_for(event.event_type)
        if route is None:
            continue
        payload = event.payload or {}
        for field in route.required_fields:
            if payload.get(field) is None:
                problems.append(
                    f"{event.event_type} is routed but its payload has no {field!r}, so the "
                    f"notification renders (unknown) for it"
                )
    return problems


def test_the_delivery_events_carry_what_their_notifications_need(db, world):
    """THE test for the defect. Runs the real delivery service and checks the events."""
    service, result, _package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()

    # A report that goes overdue, and one approaching its deadline.
    obligation = service.add_reporting_obligation(
        grant_id=grant.id, title="Q1 narrative report", due_on=_now() - timedelta(days=3)
    )
    soon = service.add_reporting_obligation(
        grant_id=grant.id, title="Q2 narrative report", due_on=_now() + timedelta(days=5),
        remind_days_before=14,
    )
    tranche = service.expect_disbursement(
        grant_id=grant.id, amount=75_000, label="First tranche", tranche_number=1,
        expected_on=_now() + timedelta(days=30),
    )
    db.commit()

    service.refresh_reporting_statuses()
    db.commit()
    service.record_report_submitted(obligation_id=soon.id, reference="ACK-1")
    db.commit()
    service.record_disbursement_received(disbursement_id=tranche.id, reference="BANK-1")
    db.commit()

    problems = _assert_payloads_satisfy_routes(_emitted(db, grant.org_id))
    assert not problems, "; ".join(sorted(set(problems)))


def test_an_overdue_notification_names_the_report(db, world):
    """The observable consequence, from an event to the rendered title.

    This is the assertion that would have failed before the fix: the notification said
    "Funder report overdue: (unknown)".
    """
    service, result, _package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()
    service.add_reporting_obligation(
        grant_id=grant.id, title="Q1 narrative report", due_on=_now() - timedelta(days=3)
    )
    db.commit()
    service.refresh_reporting_statuses()
    db.commit()

    event = db.query(models.OutboxEvent).filter(
        models.OutboxEvent.event_type == "granada:v1:report.overdue"
    ).one()

    from agent.notifications.integration import deliver_event
    from agent.notifications.providers.fake import InAppChannel

    channel = InAppChannel()
    deliver_event(db, event=event, channels=[channel])
    db.commit()

    notification = db.query(models.Notification).one()
    assert "Q1 narrative report" in notification.title, (
        f"the notification does not name the report: {notification.title!r}"
    )
    assert "(unknown)" not in notification.title
    assert "(unknown)" not in (notification.body or "")


def test_a_submission_unknown_event_carries_its_package(db, world):
    """The other CRITICAL route. Its dedupe key needs the package id, so a missing one
    would collapse every unknown submission into a single notification."""
    service, result, package = _handover(db, world)

    from agent.submission.service import SubmissionService

    org, agent, _application = world
    submission = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    from agent.submission.providers.fake import FakeSubmissionProvider

    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    submission.provider = provider
    # The package created by `_handover` is already SUBMITTED; use a fresh one so the
    # unknown path is reachable.
    fresh = _submitted_package(db, world)
    fresh.status = models.SubmissionPackage.AUTHORISED
    fresh.authorised_at = _now()
    fresh.authorised_by = org.owner_user_id
    db.commit()
    submission.provider = provider
    submission.execute(package_id=fresh.id)
    db.commit()

    problems = _assert_payloads_satisfy_routes(_emitted(db, org.id))
    assert not problems, "; ".join(sorted(set(problems)))


def test_no_routed_event_is_emitted_without_its_identity(db, world):
    """Every routed event's dedupe key resolves to something concrete.

    A key that formats to `(unknown)` would collapse the whole category into one
    notification, which is the flooding failure in reverse: everything becomes one item and
    nothing is actionable.
    """
    from agent.notifications.contract import format_route

    service, result, _package = _handover(db, world)
    grant = db.query(models.Grant).filter(models.Grant.id == result.grant_id).one()
    service.add_reporting_obligation(
        grant_id=grant.id, title="Q1", due_on=_now() - timedelta(days=1)
    )
    db.commit()
    service.refresh_reporting_statuses()
    db.commit()

    for event in _emitted(db, grant.org_id):
        route = route_for(event.event_type)
        if route is None:
            continue
        _title, _body, key = format_route(route, event.payload or {})
        assert "(unknown)" not in key, (
            f"{event.event_type} has a dedupe key that resolved to {key!r}; every "
            "occurrence would collapse into one notification"
        )
        # And the key is specific enough to distinguish two different subjects.
        assert re.search(r"[0-9a-f-]{8,}", key) or len(key) > 20, (
            f"{event.event_type}'s dedupe key {key!r} names no specific subject"
        )
