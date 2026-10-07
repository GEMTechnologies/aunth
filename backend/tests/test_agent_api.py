"""The agent and mail HTTP surface, including the approval-review contract.

The brief's §6 requires the approval API to expose enough that "the person must know
exactly what approval authorises". These tests assert that it does - field by field -
and that no endpoint can send anything.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.mail import gateway as mail_gateway  # noqa: E402
from agent.mail.providers.fake_outbound import FakeOutboundMailProvider  # noqa: E402
from agent.mail.send_service import SendService  # noqa: E402
from database import get_db  # noqa: E402
from router import get_current_user, get_tenant_context  # noqa: E402
from tenant_context import TenantContext  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "agentapi.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    mail_gateway.clear_transports()
    mail_gateway.clear_outbound_transports()
    yield
    mail_gateway.clear_transports()
    mail_gateway.clear_outbound_transports()


def _world(db, *, with_draft=True):
    """An organisation, an agent, an application, a document and an approvable intent."""
    from agent.mail.service import GranadaMail
    from agent.organisation_memory import DocumentVault, OrganisationMemory, checksum_bytes

    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="War Child Test",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()

    memory = OrganisationMemory(db, org.id)
    for key, value in (
        ("country", "Uganda"), ("organisation_type", "NGO"),
        ("organisation_name", "War Child Test"), ("registration_valid_until", "2035-01-01"),
    ):
        memory.record_fact(key=key, value=value, state=models.OrgFact.VERIFIED, source="user:1",
                           valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc))
    db.commit()

    vault = DocumentVault(db, org.id)
    document = vault.add_version(
        title="Audited Financial Statements 2026", doc_type="audited_financial_statements",
        storage_key=f"org/{org.slug}/afs.pdf", checksum_sha256=checksum_bytes(b"afs"),
        mime_type="application/pdf", valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc),
    )
    vault.approve(document, approved_by=user.id)
    db.commit()

    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()

    opportunity = models.Opportunity(
        title="Child Protection Grant 2027", source_url=f"https://unicef.org/{uuid.uuid4().hex[:8]}",
        source_name="UNICEF", country="Uganda",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        is_active=True, deadline=datetime.now(timezone.utc) + timedelta(days=30),
        created_at=datetime.now(timezone.utc),
    )
    db.add(opportunity)
    db.commit()
    application = models.Application(
        org_id=org.id, opportunity_id=opportunity.id, state="MATCHED", version=1,
        created_at=datetime.now(timezone.utc),
    )
    db.add(application)
    db.commit()

    account = models.MailAccount(
        id=str(uuid.uuid4()), org_id=org.id, agent_id=service.get().id,
        provider="FAKE", provider_account_id=f"acct-{uuid.uuid4().hex[:8]}",
        connection_type=models.MailAccount.CONNECTION_DELEGATED_OAUTH,
        address="grants@warchild.org", status=models.MailAccount.ACTIVE,
        credentials_ref="secret://fake/token", created_at=datetime.now(timezone.utc),
    )
    db.add(account)
    db.commit()

    intent = None
    if with_draft:
        thread = models.MailThread(
            id=str(uuid.uuid4()), org_id=org.id, agent_id=service.get().id,
            mail_account_id=account.id, provider_thread_id=f"t-{uuid.uuid4().hex[:8]}",
            normalized_subject="audited financial statements",
            application_id=application.id, status=models.MailThread.STATUS_OPEN,
            first_message_at=datetime.now(timezone.utc), last_message_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(thread)
        db.commit()
        draft = models.MailDraft(
            id=str(uuid.uuid4()), org_id=org.id, agent_id=service.get().id,
            thread_id=thread.id, application_id=application.id,
            subject="Re: Audited financial statements",
            body="Dear UNICEF Grants Team,\n\nPlease find our statements attached.",
            status="READY", version=1, created_at=datetime.now(timezone.utc),
            facts_used={"facts": [{"key": "organisation_name", "value": "War Child Test"}]},
            documents_used={"documents": [{"document_id": document.id,
                                           "doc_type": "audited_financial_statements"}]},
        )
        db.add(draft)
        db.commit()

        svc = SendService(db, org_id=org.id, agent_id=service.get().id)
        intent = svc.create_send_intent(
            draft=draft, to_addresses=["grants@unicef.org"], from_address="grants@warchild.org",
            mail_account_id=account.id, documents=[document],
            known_donor_domains=["unicef.org"],
        )
        db.commit()

    return {"org": org, "user": user, "service": service, "application": application,
            "document": document, "account": account, "intent": intent}


def _client(db, world, *, user=None, org_id=None):
    """A TestClient with auth and tenancy overridden onto the test session.

    Overriding the dependencies rather than minting a real token keeps the test about
    the contract. The tenant comes from the override exactly as it would come from a
    validated token in production - from the server's own resolution, never from a path
    parameter.
    """
    import main

    org_id = org_id or str(world["org"].id)
    user = user or world["user"]

    def _db_override():
        yield db

    def _tenant_override():
        # `org_ids` must be populated as well as `primary_org_id`: TenantContext
        # checks membership against `org_ids`, so passing only the primary org made
        # require_org_access answer "Access denied to organisation". That is the
        # tenancy check working correctly on a fixture that had not described a
        # membership, rather than a route bug.
        return TenantContext(
            user_id=user.id, org_ids=(org_id,), primary_org_id=org_id
        )

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(main.app)


def _clear():
    import main

    main.app.dependency_overrides.clear()


# ===========================================================================
# STATUS
# ===========================================================================
def test_the_agent_panel_reports_work_and_permissions(db):
    world = _world(db)
    client = _client(db, world)
    try:
        response = client.get("/api/v1/agent")
        assert response.status_code == 200, response.text
        body = response.json()

        # The panel's own figures.
        for field in (
            "agent_id", "display_name", "status", "autonomy", "last_active_at",
            "opportunities_evaluated_today", "matches", "applications_created",
            "active_workflows", "actions_requiring_you",
        ):
            assert field in body, f"the panel is missing {field}"

        # Mail, nested.
        assert "mail" in body
        for field in (
            "emails_received_today", "emails_sent", "emails_sent_today",
            "drafts_ready", "delivery_unknown",
        ):
            assert field in body["mail"], f"the mail panel is missing {field}"

        # Whether THIS caller may approve, so a client can grey the button rather
        # than let somebody click and be refused.
        assert body["permissions"]["may_approve_outbound_mail"] is True

        # Both autonomy switches, so "why is my agent not sending by itself?" has
        # both possible answers.
        assert body["autonomy"]["platform_enabled"] is False
        assert body["autonomy"]["organisation_enabled"] is False
        assert body["autonomy"]["high_risk_always_refused"] is True
    finally:
        _clear()


def test_health_reports_the_unpublished_outbox(db):
    """A relay that is not running is invisible otherwise: PostgreSQL holds the truth
    either way, so nothing looks broken while events pile up undelivered."""
    world = _world(db)
    client = _client(db, world)
    try:
        response = client.get("/api/v1/agent/health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert "outbox" in body and "unpublished" in body["outbox"]
        assert body["outbox"]["unpublished"] >= 1, "creating an intent staged no event"
        assert "fleet" in body and "outbound" in body
    finally:
        _clear()


# ===========================================================================
# THE APPROVAL-REVIEW CONTRACT
# ===========================================================================
def test_the_review_contract_exposes_everything_a_person_needs(db):
    """The brief's §6 list, asserted field by field.

    Never present "Approve this conversation" when the approval authorises sending a
    message the person has not seen - so the message itself is returned.
    """
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        response = client.get(f"/api/v1/agent/mail/send-intents/{intent.id}/review")
        assert response.status_code == 200, response.text
        body = response.json()

        # -- the envelope, in full --------------------------------------
        assert body["from_address"] == "grants@warchild.org"
        assert body["to_addresses"] == ["grants@unicef.org"]
        for field in ("cc_addresses", "bcc_addresses", "reply_to_address", "subject"):
            assert field in body, f"missing {field}"

        # -- THE EXACT BODY, not a preview ------------------------------
        assert body["body"] and "Please find our statements attached." in body["body"], (
            "the review does not return the message that would be sent"
        )

        # -- attachments, with the checksum the fingerprint covers ------
        assert isinstance(body["attachment_manifest"], list)

        # -- risk, with its reasons and the security flags --------------
        risk = body["risk"]
        assert risk["class"]
        assert "high_risk" in risk and "sendable_in_this_phase" in risk
        assert "reasons" in risk and "security_flags" in risk
        assert "classification" in risk

        # -- provenance --------------------------------------------------
        assert isinstance(body["facts_used"], list)
        assert isinstance(body["documents_used"], list)

        # -- context a person needs to judge -----------------------------
        assert body["donor"] == "UNICEF"
        assert body["application"]["opportunity_title"] == "Child Protection Grant 2027"
        assert body["thread"] is not None
        assert body["agent"]["display_name"]
        assert "authority_changed_since_creation" in body["agent"]

        # -- warnings and suspicious recipients --------------------------
        assert isinstance(body["warnings"], list)
        assert isinstance(body["risky_recipients"], list)

        # -- what is being authorised ------------------------------------
        assert body["message_fingerprint"]
        assert body["draft_version"] == 1
        assert body["caller_may_approve"] is True
        assert body["approval_currently_authorises_this_message"] is False
        assert body["approvals"] == []

        # A deadline key exists even when no deadline was extracted, so a client can
        # render the field unconditionally.
        assert "deadline" in body
    finally:
        _clear()


def test_the_review_shows_the_snapshot_not_the_draft(db):
    """What a person reviews must be what would be sent.

    If the review read through to the draft, editing the draft after approval would
    change the message - which is the failure the fingerprint exists to prevent, and
    the review endpoint is where it would become invisible.
    """
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        draft = db.execute(
            select(models.MailDraft).where(models.MailDraft.id == intent.draft_id)
        ).scalars().one()
        draft.body = "COMPLETELY DIFFERENT TEXT injected after the intent was frozen"
        db.commit()

        response = client.get(f"/api/v1/agent/mail/send-intents/{intent.id}/review")
        body = response.json()
        assert "Please find our statements attached." in body["body"]
        assert "COMPLETELY DIFFERENT" not in body["body"]
    finally:
        _clear()


def test_a_non_approver_is_told_they_may_not_approve(db):
    """Grey the button rather than let somebody click and be refused."""
    world = _world(db)
    outsider = models.User(id=str(uuid.uuid4()), display_name="Viewer")
    db.add(outsider)
    db.commit()

    client = _client(db, world, user=outsider)
    try:
        response = client.get(f"/api/v1/agent/mail/send-intents/{world['intent'].id}/review")
        assert response.status_code == 200
        body = response.json()
        assert body["caller_may_approve"] is False
        assert body["caller_may_approve_reason"]
    finally:
        _clear()


# ===========================================================================
# DECISIONS
# ===========================================================================
def test_approving_records_a_decision_bound_to_the_fingerprint(db):
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        response = client.post(f"/api/v1/agent/mail/send-intents/{intent.id}/approve",
                               json={"note": "reads well"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"] == "APPROVE"
        assert body["fingerprint"] == intent.message_fingerprint

        db.expire_all()
        fresh = db.execute(
            select(models.MailSendIntent).where(models.MailSendIntent.id == intent.id)
        ).scalars().one()
        assert fresh.status == models.MailSendIntent.APPROVED

        review = client.get(f"/api/v1/agent/mail/send-intents/{intent.id}/review").json()
        assert review["approval_currently_authorises_this_message"] is True
    finally:
        _clear()


def test_a_non_approver_cannot_approve(db):
    world = _world(db)
    outsider = models.User(id=str(uuid.uuid4()), display_name="Viewer")
    db.add(outsider)
    db.commit()

    client = _client(db, world, user=outsider)
    try:
        response = client.post(
            f"/api/v1/agent/mail/send-intents/{world['intent'].id}/approve", json={}
        )
        assert response.status_code == 403, response.text
    finally:
        _clear()


def test_a_cross_tenant_user_cannot_approve(db):
    from tests.test_mail import _org_and_agent

    world = _world(db)
    other_org, other_service = _org_and_agent(db, name="Tenant B", slug="tenant-b")
    other_user = db.execute(
        select(models.User).where(models.User.id == other_org.owner_user_id)
    ).scalars().one()

    client = _client(db, world, user=other_user)
    try:
        response = client.post(
            f"/api/v1/agent/mail/send-intents/{world['intent'].id}/approve", json={}
        )
        assert response.status_code == 403, response.text
    finally:
        _clear()


def test_a_high_risk_message_is_refused_with_its_code(db):
    """Even an owner clicking approve cannot send a banking message."""
    world = _world(db)
    intent = world["intent"]
    intent.risk_class = "BANKING"
    db.commit()

    client = _client(db, world)
    try:
        response = client.post(f"/api/v1/agent/mail/send-intents/{intent.id}/approve", json={})
        assert response.status_code == 409, response.text
        assert "HIGH_RISK_ACTION_BLOCKED" in response.text
    finally:
        _clear()


def test_reject_and_request_changes(db):
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        response = client.post(
            f"/api/v1/agent/mail/send-intents/{intent.id}/request-changes",
            json={"note": "too formal"},
        )
        assert response.status_code == 200, response.text
        db.expire_all()
        fresh = db.execute(
            select(models.MailSendIntent).where(models.MailSendIntent.id == intent.id)
        ).scalars().one()
        assert fresh.status == models.MailSendIntent.CHANGES_REQUESTED
        assert "too formal" in (fresh.status_reason or "")
    finally:
        _clear()


def test_cancel_revokes_the_approval(db):
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        assert client.post(
            f"/api/v1/agent/mail/send-intents/{intent.id}/approve", json={}
        ).status_code == 200

        response = client.post(
            f"/api/v1/agent/mail/send-intents/{intent.id}/cancel",
            json={"note": "wrong funder"},
        )
        assert response.status_code == 200, response.text

        db.expire_all()
        fresh = db.execute(
            select(models.MailSendIntent).where(models.MailSendIntent.id == intent.id)
        ).scalars().one()
        assert fresh.status == models.MailSendIntent.CANCELLED
        attend = db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id
            )
        ).scalars().all()
        assert all(a.status == models.MailApproval.STATUS_REVOKED for a in attend), (
            "cancelling left a live approval behind for a later retry to find"
        )
    finally:
        _clear()


def test_the_review_queue_shows_what_needs_a_person(db):
    world = _world(db)
    client = _client(db, world)
    try:
        response = client.get("/api/v1/agent/mail/send-intents")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        entry = body["send_intents"][0]
        assert entry["id"] == world["intent"].id
        assert entry["approval_currently_authorises_this_message"] is False
        # The queue does NOT carry the body: a list is for choosing.
        assert "body" not in entry
    finally:
        _clear()


def test_the_draft_endpoints_work_and_the_list_omits_the_body(db):
    world = _world(db)
    intent = world["intent"]
    client = _client(db, world)
    try:
        listing = client.get("/api/v1/agent/mail/drafts")
        assert listing.status_code == 200
        body = listing.json()
        assert body["count"] == 1
        assert "body_preview" in body["drafts"][0]
        assert "body" not in body["drafts"][0], "the list ships whole bodies to render a table"

        one = client.get(f"/api/v1/agent/mail/drafts/{intent.draft_id}")
        assert one.status_code == 200
        assert "Please find our statements attached." in one.json()["body"]
    finally:
        _clear()


def test_the_accounts_endpoint_never_returns_a_secret(db):
    """``credentials_ref`` is a pointer, and no secret value is returned.

    The schema has no column that could hold a provider password; this asserts the API
    does not leak whatever the pointer names.
    """
    world = _world(db)
    client = _client(db, world)
    try:
        response = client.get("/api/v1/agent/mail/accounts")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        account = body["accounts"][0]
        assert account["has_credentials_ref"] is True
        # The pointer is not echoed, and neither is anything secret-shaped.
        serialised = str(body).lower()
        assert "secret://" not in serialised
        assert "token" not in serialised or "credentials" not in serialised
        # Capability configuration is resolved live so a missing transport is visible.
        assert "inbound_configured" in account and "outbound_configured" in account
    finally:
        _clear()


# ===========================================================================
# NOTHING HERE SENDS
# ===========================================================================
def test_no_agent_endpoint_can_send_a_message(db):
    """Approval is a permission, not an execution.

    The whole surface is enumerated: approving, cancelling, reconciling and reading
    must all leave the provider untouched. Only the send pipeline sends, and only
    after its own final authority check.
    """
    world = _world(db)
    outbound = FakeOutboundMailProvider()
    mail_gateway.register_outbound_transport("FAKE", outbound)
    intent = world["intent"]
    client = _client(db, world)
    try:
        client.post(f"/api/v1/agent/mail/send-intents/{intent.id}/approve", json={})
        client.get(f"/api/v1/agent/mail/send-intents/{intent.id}/review")
        client.get("/api/v1/agent/mail/send-intents")
        client.get("/api/v1/agent")
        client.get("/api/v1/agent/health")
        client.get("/api/v1/agent/mail/accounts")
        client.get("/api/v1/agent/mail/drafts")
        client.post(f"/api/v1/agent/mail/send-intents/{intent.id}/reconcile")

        assert outbound.call_count == 0, "an API endpoint called the provider"
        assert outbound.submission_count == 0, "an API endpoint sent a message"
    finally:
        _clear()


def test_the_router_declares_no_send_route():
    """A structural check, so a future endpoint cannot quietly add one."""
    import main

    paths = {getattr(r, "path", "") for r in main.app.routes}
    agent_paths = {p for p in paths if "/agent" in p}
    assert agent_paths, "no agent routes were registered"

    # Compared on the FINAL path segment, because `/send-intents` is a resource name
    # rather than a send route. A substring check flagged it, which is the kind of
    # over-broad assertion that gets deleted instead of fixed.
    forbidden_segments = {"send", "submit", "force", "approve-and-send", "execute", "dispatch"}
    for path in agent_paths:
        segments = {s.strip("{}") for s in path.split("/") if s}
        overlap = segments & forbidden_segments
        assert not overlap, (
            f"the agent surface exposes {path} (segment(s) {sorted(overlap)}); approval "
            "is a permission, not an execution, and sending belongs to the send "
            "pipeline alone"
        )
