"""The verification pipeline against a real database.

What matters here is not that a code is found - `test_mail_verification.py` covers the extraction -
but that a code cannot be applied to the WRONG request, cannot be applied TWICE, and cannot be
applied after it has expired. Those are the failures that damage an account.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import models
from agent.mail.verification_service import VerificationService
from conftest import make_sqlite_db

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "verification.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="Globalmotion", slug=None):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name=name, slug=slug or f"org-{uuid.uuid4().hex[:6]}",
        owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    return org.id


def _service(db, org_id):
    return VerificationService(db, org_id=org_id)


MAIL = dict(
    subject="Verify your UN Partner Portal account",
    body="Your verification code is 483920. It expires in 30 minutes.",
    sender="no-reply@mail.unpartnerportal.org",
    recipients=["globalmotion@granadac.com"],
)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_matching_message_satisfies_the_request(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    result = svc.examine(**MAIL, received_at=NOW)
    db.commit()

    assert result.satisfied
    assert result.outcome.code == "483920"
    assert result.request_id == req.id

    row = db.query(models.MailVerificationRequest).filter_by(id=req.id).first()
    assert row.status == models.MailVerificationRequest.SATISFIED
    assert row.result_kind == "CODE"
    assert row.result_code == "483920"
    assert row.satisfied_at is not None
    assert row.fingerprint


def test_the_result_is_readable_by_the_waiting_task(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration", browser_task_id="task-123")
    db.commit()
    svc.examine(**MAIL, received_at=NOW)
    db.commit()

    out = svc.result_for(req.id)
    assert out["ready"] is True
    assert out["code"] == "483920"
    assert out["kind"] == "CODE"


def test_a_pending_request_reports_why_it_is_not_ready(db):
    """'not yet' and 'no such request' lead to different actions, so they must not both be None."""
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    out = svc.result_for(req.id)
    assert out["ready"] is False
    assert out["status"] == models.MailVerificationRequest.PENDING
    assert out["detail"]

    assert svc.result_for("nope")["status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Refusals - these are the ones that protect the account
# ---------------------------------------------------------------------------
def test_a_message_to_another_mailbox_does_not_satisfy(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    result = svc.examine(**{**MAIL, "recipients": ["other-ngo@granadac.com"]}, received_at=NOW)
    db.commit()

    assert not result.satisfied
    assert db.query(models.MailVerificationRequest).filter_by(id=req.id).first().status == "PENDING"


def test_a_message_from_another_portal_does_not_satisfy(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    result = svc.examine(**{**MAIL, "sender": "no-reply@evil-unpartnerportal.org"}, received_at=NOW)
    db.commit()

    assert not result.satisfied
    assert db.query(models.MailVerificationRequest).filter_by(id=req.id).first().status == "PENDING"


def test_a_message_arriving_after_the_window_does_not_satisfy(db):
    """The message is outside the request's window, so it is refused. The request itself is still
    live by the wall clock, which is why its status is PENDING and not EXPIRED - the two are
    different facts and conflating them is what makes an operator think the mail never arrived."""
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration", ttl=timedelta(seconds=1))
    db.commit()

    result = svc.examine(**MAIL, received_at=NOW + timedelta(minutes=10))
    db.commit()

    assert not result.satisfied
    row = db.query(models.MailVerificationRequest).filter_by(id=req.id).first()
    assert row.status != models.MailVerificationRequest.SATISFIED
    assert row.last_reason


def test_a_request_past_its_expiry_is_marked_expired(db):
    """Backdated directly, because the expiry that matters is the one on the row."""
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    req.expires_at = NOW - timedelta(minutes=1)
    db.commit()

    assert svc.pending() == []
    db.commit()
    assert db.query(models.MailVerificationRequest).filter_by(id=req.id).first().status == "EXPIRED"


def test_a_second_organisation_is_not_satisfied_by_the_first_ones_code(db):
    """TWO NGOs REGISTERING ON THE SAME PORTAL IS THE NORMAL CASE, not an exotic one. Tenant
    isolation is what keeps their codes apart."""
    a = _org(db, "Globalmotion", "globalmotion")
    b = _org(db, "War Child", "war-child")
    svc_a = _service(db, a)
    svc_b = _service(db, b)
    req_a = svc_a.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                          purpose="ngo registration")
    req_b = svc_b.request(address="warchild@granadac.com", portal_host="unpartnerportal.org",
                          purpose="ngo registration")
    db.commit()

    svc_a.examine(**MAIL, received_at=NOW)
    db.commit()

    assert db.query(models.MailVerificationRequest).filter_by(id=req_a.id).first().status == "SATISFIED"
    assert db.query(models.MailVerificationRequest).filter_by(id=req_b.id).first().status == "PENDING"


def test_a_duplicate_delivery_is_not_a_second_satisfaction(db):
    """A retried job or a re-delivered message must not cause the code to be applied twice."""
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    first = svc.examine(**MAIL, received_at=NOW)
    db.commit()
    second = svc.examine(**MAIL, received_at=NOW + timedelta(seconds=5))
    db.commit()

    assert first.satisfied
    assert not second.satisfied
    rows = db.query(models.MailVerificationRequest).filter_by(org_id=org).all()
    assert len(rows) == 1, "a duplicate delivery must not create a second request"
    assert rows[0].status == "SATISFIED"


def test_a_code_arriving_before_the_request_is_refused(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    result = svc.examine(**MAIL, received_at=NOW - timedelta(minutes=20))
    db.commit()

    assert not result.satisfied
    assert db.query(models.MailVerificationRequest).filter_by(id=req.id).first().status == "PENDING"


# ---------------------------------------------------------------------------
# Diagnosis - a request that silently stays pending is indistinguishable from a broken pipeline
# ---------------------------------------------------------------------------
def test_an_unusable_message_records_what_was_seen(db):
    org = _org(db)
    svc = _service(db, org)
    req = svc.request(address="globalmotion@granadac.com", portal_host="unpartnerportal.org",
                      purpose="ngo registration")
    db.commit()

    svc.examine(**{**MAIL, "subject": "Newsletter", "body": "No code here."}, received_at=NOW)
    db.commit()

    row = db.query(models.MailVerificationRequest).filter_by(id=req.id).first()
    assert row.status == "PENDING"
    assert row.examined == 1
    assert row.last_reason, "the operator needs to know the mail arrived and was not usable"


def test_the_service_refuses_an_unknown_tenant():
    with pytest.raises(ValueError):
        VerificationService(None, org_id="")
