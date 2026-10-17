"""Reading mail: the product surface that did not exist.

Inbound mail was stored correctly - tenant-scoped, labelled with the mailbox it arrived at - and no
route read it. Every mail route in the module concerned OUTBOUND send-intents, so a code arriving at
an NGO's portal mailbox was visible only to an engineer with psql.

THE LOAD-BEARING ASSERTION IS THE REDACTION ONE. A one-time code is a credential for the length of
its window: whoever reads it can complete - or hijack - the registration it belongs to. The browser
worker already treats a code as a per-run secret; these tests require the read surface to do the
same, so a screenshot of an inbox is not a screenshot of somebody's pending account.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from database import get_db  # noqa: E402
from router import get_current_user, get_tenant_context  # noqa: E402
from tenant_context import TenantContext  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "mailread.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="Granada Test Organisation",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    # Provisioned through the service rather than constructed by hand: the agent row has required
    # columns a fixture would have to guess, and guessing them is how a fixture stops resembling
    # what production writes.
    from agent.decision.policy import Autonomy
    from agent.granada_agent import GranadaAgentService

    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()
    agent = service.get()
    return {"org": org, "user": user, "agent": agent}


def mailbox(db, world, *, address, marker=None, primary=False, status="ACTIVE"):
    identity = models.MailIdentity(
        id=str(uuid.uuid4()), org_id=world["org"].id, agent_id=world["agent"].id,
        address=address, display_name=marker, is_primary=primary, status=status,
        identity_type=models.MailIdentity.TYPE_MANAGED, token=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc),
    )
    db.add(identity)
    db.commit()
    return identity


def message(db, world, *, identity, subject, preview, sender="no-reply@mail.unpartnerportal.org",
            direction=models.MailMessage.DIRECTION_INBOUND, created_at=None):
    row = models.MailMessage(
        id=str(uuid.uuid4()), org_id=world["org"].id, agent_id=world["agent"].id,
        mail_identity_id=identity.id if identity else None,
        provider_message_id=f"<{uuid.uuid4().hex}@test>",
        direction=direction, sender=sender, recipients=[identity.address if identity else None],
        subject=subject, body_preview=preview,
        received_at=created_at or datetime.now(timezone.utc),
        created_at=created_at or datetime.now(timezone.utc),
        processing_status=models.MailMessage.PROCESSING_PERSISTED,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def client(db, world):
    import main

    org_id = str(world["org"].id)
    user = world["user"]

    def _db_override():
        yield db

    def _tenant_override():
        return TenantContext(user_id=user.id, org_ids=(org_id,), primary_org_id=org_id)

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Mailboxes
# ---------------------------------------------------------------------------
def test_an_organisation_with_no_mailboxes_reports_none(client):
    body = client.get("/api/v1/agent/mail/mailboxes").json()
    assert body["total"] == 0
    assert body["mailboxes"] == []


def test_each_mailbox_reports_its_purpose_in_words(client, db, world):
    mailbox(db, world, address="ngo-a-1a2b@granadac.com",
            marker="portal-registration:www.unpartnerportal.org")
    mailbox(db, world, address="ngo-main@granadac.com", primary=True)

    rows = client.get("/api/v1/agent/mail/mailboxes").json()["mailboxes"]
    purposes = {row["address"]: row["purpose"] for row in rows}
    assert "www.unpartnerportal.org" in purposes["ngo-a-1a2b@granadac.com"]
    assert "public" in purposes["ngo-main@granadac.com"].lower()


def test_a_mailbox_reports_how_much_mail_it_holds(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    for index in range(3):
        message(db, world, identity=box, subject=f"Notice {index}", preview="hello")

    rows = client.get("/api/v1/agent/mail/mailboxes").json()["mailboxes"]
    assert rows[0]["message_count"] == 3
    assert rows[0]["last_message_at"] is not None


def test_messages_with_no_mailbox_are_counted_separately(client, db, world):
    """An unlabelled message is honest - it arrived before the mailbox was recorded - and it must not
    vanish from the count just because it has no mailbox."""
    mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=None, subject="Old message", preview="no mailbox")

    body = client.get("/api/v1/agent/mail/mailboxes").json()
    assert body["unassigned_messages"] == 1


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def test_messages_come_back_newest_first(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for index in range(3):
        message(db, world, identity=box, subject=f"Msg {index}", preview="x",
                created_at=base + timedelta(hours=index))

    rows = client.get("/api/v1/agent/mail/messages").json()["messages"]
    assert [r["subject"] for r in rows] == ["Msg 2", "Msg 1", "Msg 0"]


def test_messages_can_be_filtered_to_one_mailbox(client, db, world):
    one = mailbox(db, world, address="one@granadac.com")
    two = mailbox(db, world, address="two@granadac.com")
    message(db, world, identity=one, subject="For one", preview="x")
    message(db, world, identity=two, subject="For two", preview="x")

    rows = client.get(f"/api/v1/agent/mail/messages?mailbox_id={one.id}").json()["messages"]
    assert [r["subject"] for r in rows] == ["For one"]
    assert rows[0]["mailbox_address"] == "one@granadac.com"


def test_an_invalid_direction_is_a_400_not_an_empty_list(client):
    """An empty list would read as "no such mail", which is a different and wrong answer."""
    resp = client.get("/api/v1/agent/mail/messages?direction=SIDEWAYS")
    assert resp.status_code == 400


def test_direction_filtering_works(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject="In", preview="x")
    message(db, world, identity=box, subject="Out", preview="x",
            direction=models.MailMessage.DIRECTION_OUTBOUND)

    rows = client.get("/api/v1/agent/mail/messages?direction=INBOUND").json()["messages"]
    assert [r["subject"] for r in rows] == ["In"]


def test_bodies_are_not_returned_only_a_reference_flag(client, db, world):
    """`mail_messages` holds a reference, not the body: reading fifty bodies out of object storage
    for a list view is both slow and a much larger exposure than the list needs."""
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject="Has body", preview="preview text")
    row = client.get("/api/v1/agent/mail/messages").json()["messages"][0]
    assert row["has_body"] is False
    assert row["preview"] == "preview text"


def test_pagination_pages_without_repeating(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for index in range(5):
        message(db, world, identity=box, subject=f"M{index}", preview="x",
                created_at=base + timedelta(minutes=index))

    first = client.get("/api/v1/agent/mail/messages?limit=2").json()
    assert len(first["messages"]) == 2 and first["has_more"] is True

    second = client.get(
        f"/api/v1/agent/mail/messages?limit=2&before={first['next_before']}"
    ).json()
    first_ids = {r["id"] for r in first["messages"]}
    second_ids = {r["id"] for r in second["messages"]}
    assert first_ids.isdisjoint(second_ids), "the cursor must not repeat a row"


def test_the_limit_is_bounded(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject="only", preview="x")
    assert len(client.get("/api/v1/agent/mail/messages?limit=100000").json()["messages"]) == 1


# ---------------------------------------------------------------------------
# THE REDACTION - a code is a credential
# ---------------------------------------------------------------------------
def test_a_verification_code_is_redacted_from_the_preview(client, db, world):
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box,
            subject="Verify your UN Partner Portal account",
            preview="Thank you for registering. Your verification code is 481920.")

    row = client.get("/api/v1/agent/mail/messages").json()["messages"][0]
    assert "481920" not in (row["preview"] or ""), "a one-time code must never reach a client"
    assert "[code redacted]" in row["preview"]
    assert row["code_redacted"] is True
    # The message is still recognisable: "a code arrived" is the useful fact.
    assert "verif" in (row["subject"] or "").lower()


def test_an_ordinary_number_is_not_redacted(client, db, world):
    """Masking every four-digit number would corrupt invoices and grant amounts and teach nobody
    anything."""
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject="Invoice 2026",
            preview="Total due: 481920 within 30 days")

    row = client.get("/api/v1/agent/mail/messages").json()["messages"][0]
    assert row["preview"] == "Total due: 481920 within 30 days"
    assert row["code_redacted"] is False


@pytest.mark.parametrize(
    "subject,preview",
    [
        ("Confirm your email", "Use 1234 to continue"),
        ("Your one-time password", "OTP: 998877"),
        ("Activation code inside", "Enter 555444 now"),
        ("Security code", "The code you requested is 246810"),
    ],
)
def test_every_verification_wording_is_caught(client, db, world, subject, preview):
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject=subject, preview=preview)
    row = client.get("/api/v1/agent/mail/messages").json()["messages"][0]
    assert "[code redacted]" in row["preview"]


def test_a_longer_number_is_not_partially_masked(client, db, world):
    """Word-bounded matching, so a phone number inside a verification email does not become
    '[code redacted]7'."""
    box = mailbox(db, world, address="box@granadac.com")
    message(db, world, identity=box, subject="Verify your account",
            preview="Call +256772123456 or use your code")
    row = client.get("/api/v1/agent/mail/messages").json()["messages"][0]
    assert "256772123456" in row["preview"]


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
def test_another_organisations_mail_is_invisible(db, world):
    import main

    stranger = models.User(id=str(uuid.uuid4()), display_name="Stranger")
    db.add(stranger)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other", slug=f"org-{uuid.uuid4().hex[:8]}",
        owner_user_id=stranger.id,
    )
    db.add(other)
    db.commit()

    box = mailbox(db, world, address="victim@granadac.com")
    message(db, world, identity=box, subject="Private", preview="secret")

    def _db_override():
        yield db

    def _tenant_override():
        return TenantContext(user_id=stranger.id, org_ids=(str(other.id),),
                             primary_org_id=str(other.id))

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: stranger
    try:
        client = TestClient(main.app)
        body = client.get("/api/v1/agent/mail/messages").json()
        assert body["messages"] == [], "another tenant's mail must not be readable"
    finally:
        main.app.dependency_overrides.clear()
