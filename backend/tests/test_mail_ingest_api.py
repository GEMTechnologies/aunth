"""The inbound doorway: authentication, and the three answers it can give.

The distinctions tested here are the ones that lose mail when they are collapsed. A disabled mailbox
and an unknown one are both `reject` (the sender deserves a bounce) but for different reasons an
operator needs to tell apart. A TEMPORARY backend failure is `retry`, never `reject` - bouncing a
funder's message because of a database blip is the worst outcome available.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import mail_ingest_api

KEY = "test-ingest-key-0123456789"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mail_ingest_api.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config.settings, "mail_ingest_key", KEY, raising=False)
    yield KEY


def post(client, body, key=KEY):
    headers = {"X-Ingest-Key": key} if key else {}
    return client.post("/api/v1/internal/mail/ingest", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Authentication - the endpoint is closed by default
# ---------------------------------------------------------------------------
def test_an_unconfigured_deployment_refuses_rather_than_accepting(monkeypatch, client):
    monkeypatch.setattr(config.settings, "mail_ingest_key", "", raising=False)
    resp = post(client, {"to": "ngo@granadac.com"})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_a_missing_key_is_refused(client, configured):
    assert post(client, {"to": "ngo@granadac.com"}, key=None).status_code == 401


def test_a_wrong_key_is_refused(client, configured):
    assert post(client, {"to": "ngo@granadac.com"}, key="wrong").status_code == 401


def test_a_truncated_key_is_refused(client, configured):
    assert post(client, {"to": "ngo@granadac.com"}, key=KEY[:-1]).status_code == 401


def test_health_reports_configuration_but_never_the_secret(client, configured):
    body = client.get("/api/v1/internal/mail/ingest/health").json()
    assert body["configured"] is True
    assert KEY not in str(body)


# ---------------------------------------------------------------------------
# Rejections that do not need a database
# ---------------------------------------------------------------------------
def test_a_foreign_domain_is_rejected(client, configured):
    out = post(client, {"to": "someone@example.org"}).json()
    assert out["action"] == "reject"
    assert "not a Granada mail domain" in out["reason"]


def test_an_address_with_no_at_sign_is_rejected(client, configured):
    out = post(client, {"to": "not-an-address"}).json()
    assert out["action"] == "reject"


@pytest.mark.parametrize(
    "address",
    ["noreply@granadac.com", "no-reply@granadac.com", "postmaster@granadac.com",
     "abuse@granadac.com", "mailer-daemon@granadac.com"],
)
def test_send_only_addresses_are_never_accepted_into(client, configured, address):
    """A reply to no-reply is not a conversation, and accepting it would put it in an NGO's mailbox."""
    out = post(client, {"to": address}).json()
    assert out["action"] == "reject"
    assert "send-only" in out["reason"]


def test_an_oversized_message_is_rejected(client, configured):
    out = post(client, {"to": "ngo@granadac.com", "text": "x" * (mail_ingest_api.MAX_BODY + 1)}).json()
    assert out["action"] == "reject"
    assert "exceeds the size" in out["reason"]


# ---------------------------------------------------------------------------
# The transient case - the one that must never bounce
# ---------------------------------------------------------------------------
def test_a_backend_failure_asks_for_a_retry_not_a_bounce(client, configured, monkeypatch):
    """A database blip must not bounce a funder's message. `reject` here would be the worst outcome
    available, and `accept` would claim we stored something we did not."""
    def exploding_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(mail_ingest_api, "SESSION_FACTORY", exploding_session)
    out = post(client, {"to": "ngo@granadac.com", "subject": "Verify"}).json()
    assert out["action"] == "retry"
    assert "temporary" in out["reason"]


def test_a_retry_response_never_claims_the_message_was_stored(client, configured, monkeypatch):
    monkeypatch.setattr(
        mail_ingest_api, "SESSION_FACTORY",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    out = post(client, {"to": "ngo@granadac.com"}).json()
    assert out["stored_message_id"] is None
    assert out["verification_ready"] is False
