"""Phase 7c: the gates on unattended sending. Every one must fail closed."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.mail import gateway as mail_gateway  # noqa: E402
from agent.mail.approval import ApprovalService  # noqa: E402
from agent.mail.autonomy import (  # noqa: E402
    AUTONOMOUS_CLASSIFICATION_ALLOWLIST,
    AUTONOMOUS_RISK_ALLOWLIST,
    AutonomousPolicy,
    disable_for_organisation,
    enable_for_organisation,
)
from agent.mail.ceiling import OutboundRisk  # noqa: E402
from agent.mail.providers.fake_outbound import FakeOutboundMailProvider  # noqa: E402
from agent.mail.send_service import SendService  # noqa: E402
from agent.mail.vocabulary import MailClassification  # noqa: E402

from tests.test_mail import (  # noqa: E402
    _mailbox,
    _opportunity_and_application,
    _org_and_agent,
)
from tests.test_mail_outbound import _draft, _identity  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "autonomy.db")
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


@pytest.fixture
def platform_on(monkeypatch):
    """Turn the platform kill switch ON for one test.

    Every unit test above passes ``platform_enabled`` explicitly so it can assert the
    gate. The end-to-end tests exercise the PRODUCTION path, which reads the setting -
    so they have to actually flip it, and that is the point: the switch is
    authoritative and there is no way around it.
    """
    from config import settings

    monkeypatch.setattr(settings, "autonomous_mail_enabled", True, raising=False)
    return True


#: A message that passes every gate. Each test breaks exactly one thing.
GOOD = dict(
    risk_class=OutboundRisk.ROUTINE.value,
    classification=MailClassification.ACKNOWLEDGEMENT.value,
    recipients=["grants@unicef.org"],
    thread_participants=["grants@unicef.org"],
    known_donor_domains=["unicef.org"],
    security_flags=[],
)


def _policy(db, service):
    return AutonomousPolicy(db, org_id=service.org_id, agent_id=service.get().id)


def _opted_in(db, service, *, daily_limit=None, autonomy=None):
    """Opt in, and raise the autonomy level.

    Both, because opting into unattended mail while at MONITOR_ONLY is a
    contradiction: the level gate refuses it, correctly. Real provisioning would set
    both together, so the fixture does too - and `test_a_low_autonomy_level_never_
    sends_unattended` covers the case where only the opt-in is present.
    """
    from agent.decision.policy import Autonomy

    enable_for_organisation(db, org_id=service.org_id, daily_limit=daily_limit)
    service.get().autonomy = autonomy or Autonomy.AUTO_ROUTINE
    db.commit()


# ===========================================================================
# THE KILL SWITCH
# ===========================================================================
def test_the_platform_switch_defaults_to_off():
    """On every deployment, until somebody deliberately turns it on.

    This is the single most important property of the phase: the capability is inert
    by default, and it is one setting rather than a per-organisation scatter.
    """
    from agent.mail.autonomy import DEFAULT_AUTONOMOUS_MAIL_ENABLED, platform_autonomy_enabled

    assert DEFAULT_AUTONOMOUS_MAIL_ENABLED is False
    assert platform_autonomy_enabled() is False


def test_nothing_is_autonomous_while_the_platform_switch_is_off(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)

    decision = _policy(db, service).evaluate(platform_enabled=False, **GOOD)
    assert decision.allowed is False
    assert decision.code == "AUTONOMOUS_MAIL_DISABLED"


def test_the_platform_switch_alone_is_not_enough(db):
    """An organisation that has not opted in can never receive an unattended send."""
    org, service = _org_and_agent(db)
    decision = _policy(db, service).evaluate(platform_enabled=True, **GOOD)
    assert decision.allowed is False
    assert decision.code == "AUTONOMOUS_NOT_ENABLED_FOR_ORGANISATION"


# ===========================================================================
# EVERY GATE
# ===========================================================================
def test_every_gate_passes_for_an_eligible_message(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(platform_enabled=True, **GOOD)
    assert decision.allowed is True, decision.as_dict()
    assert decision.code == "AUTONOMOUS_SEND_PERMITTED"
    assert all(decision.gate_results.values())


@pytest.mark.parametrize("level", ("MONITOR_ONLY", "DRAFT_ONLY"))
def test_a_low_autonomy_level_never_sends_unattended(db, level):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    service.get().autonomy = level
    db.commit()

    decision = _policy(db, service).evaluate(platform_enabled=True, **GOOD)
    assert decision.allowed is False
    assert decision.code == "AUTONOMY_LEVEL_TOO_LOW"


def test_a_paused_agent_never_sends_unattended(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    service.get().status = models.GranadaAgent.PAUSED
    db.commit()
    decision = _policy(db, service).evaluate(platform_enabled=True, **GOOD)
    assert decision.allowed is False
    assert decision.code == "AGENT_NOT_ACTIVE"


@pytest.mark.parametrize(
    "risk",
    ("BANKING", "FINANCIAL", "CONTRACT_RELATED", "LEGAL", "CREDENTIAL_SECURITY"),
)
def test_high_risk_is_never_autonomous(db, risk):
    """Already unsendable with a human approval; certainly not without one."""
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True, **{**GOOD, "risk_class": risk}
    )
    assert decision.allowed is False
    assert decision.code == "HIGH_RISK_ACTION_BLOCKED"


def test_a_risk_class_outside_the_allowlist_is_refused(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True, **{**GOOD, "risk_class": OutboundRisk.AWARD_RELATED.value}
    )
    assert decision.allowed is False
    assert decision.code == "RISK_CLASS_NOT_AUTONOMOUS"
    assert OutboundRisk.AWARD_RELATED.value not in AUTONOMOUS_RISK_ALLOWLIST


@pytest.mark.parametrize(
    "classification",
    (
        MailClassification.AWARD_NOTICE.value,
        MailClassification.REJECTION_NOTICE.value,
        MailClassification.CONTRACT.value,
        MailClassification.BANK_DETAIL_REQUEST.value,
        MailClassification.DEADLINE_CHANGE.value,
        MailClassification.DOCUMENT_REQUEST.value,
        MailClassification.UNKNOWN.value,
    ),
)
def test_classifications_that_need_a_person_are_refused(db, classification):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True, **{**GOOD, "classification": classification}
    )
    assert decision.allowed is False
    assert decision.code == "CLASSIFICATION_REQUIRES_HUMAN"


def test_a_missing_classification_is_not_permission(db):
    """Absence is not consent. An unclassified message cannot be sent unattended."""
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True, **{**GOOD, "classification": None}
    )
    assert decision.allowed is False
    assert decision.code == "NO_CLASSIFICATION"


def test_the_classification_allowlist_is_narrow():
    """AWARD and REJECTION are conspicuously absent, and that is the point."""
    assert MailClassification.AWARD_NOTICE.value not in AUTONOMOUS_CLASSIFICATION_ALLOWLIST
    assert MailClassification.REJECTION_NOTICE.value not in AUTONOMOUS_CLASSIFICATION_ALLOWLIST
    assert MailClassification.DOCUMENT_REQUEST.value not in AUTONOMOUS_CLASSIFICATION_ALLOWLIST
    assert MailClassification.CONTRACT.value not in AUTONOMOUS_CLASSIFICATION_ALLOWLIST


# ---------------------------------------------------------------------------
# The most important gate: the recipient
# ---------------------------------------------------------------------------
def test_a_new_recipient_is_never_eligible(db):
    """THE gate. A stranger must never receive an unattended email in the org's name.

    The address would necessarily come from inference rather than correspondence, and
    emailing the wrong party cannot be undone.
    """
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True,
        **{**GOOD, "recipients": ["grants@somewhere-new.example"],
           "thread_participants": ["grants@unicef.org"]},
    )
    assert decision.allowed is False
    assert decision.code == "UNKNOWN_RECIPIENT"


def test_a_known_domain_is_eligible_even_if_not_yet_a_participant(db):
    """A new *address* at a funder already corresponded with is a normal reply."""
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True,
        **{**GOOD, "recipients": ["new.officer@unicef.org"], "thread_participants": []},
    )
    assert decision.allowed is True, decision.as_dict()


def test_an_impersonated_domain_is_not_a_known_domain(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True,
        **{**GOOD, "recipients": ["grants@unicef-portal.example"]},
    )
    assert decision.allowed is False
    assert decision.code == "UNKNOWN_RECIPIENT"


def test_an_empty_recipient_list_is_refused(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True, **{**GOOD, "recipients": []}
    )
    assert decision.allowed is False
    assert decision.code == "NO_RECIPIENT"


# ---------------------------------------------------------------------------
# Security flags and the ceiling
# ---------------------------------------------------------------------------
def test_a_security_flagged_message_cannot_send_unattended(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    decision = _policy(db, service).evaluate(
        platform_enabled=True,
        **{**GOOD, "security_flags": ["PROMPT_INJECTION_ATTEMPT"]},
    )
    assert decision.allowed is False
    assert decision.code == "SECURITY_FLAGS_PRESENT"


def test_the_daily_ceiling_stops_a_misclassification_becoming_a_hundred_emails(
    db, platform_on
):
    """After the ceiling, the next message waits for a person.

    Driven through the REAL path rather than by inserting approval rows, because the
    first version fabricated a `send_intent_id` and passed only while SQLite's foreign
    keys happened to be off - it passed in isolation and failed in the full suite.
    A test that relies on a constraint being disabled is testing the wrong system, and
    the ceiling counts real approvals, so the test creates real ones.
    """
    org, service = _org_and_agent(db)
    _opportunity_and_application(db, org)
    _opted_in(db, service, daily_limit=3)
    outbound = FakeOutboundMailProvider()
    account = _mailbox(db, service)
    identity = _identity(db, service)
    svc = SendService(db, org_id=org.id, agent_id=service.get().id, outbound=outbound)

    def eligible_intent(n: int):
        draft = _draft(
            db, service, version=n,
            body="Thank you for your message. We have received your application.",
            subject=f"Re: your application ({n})",
        )
        return svc.create_send_intent(
            draft=draft, to_addresses=["grants@unicef.org"], from_address=identity.address,
            mail_account_id=account.id, mail_identity_id=identity.id,
            known_donor_domains=["unicef.org"], autonomous=True,
            classification=MailClassification.ACKNOWLEDGEMENT.value,
            thread_participants=["grants@unicef.org"],
        )

    for n in range(3):
        intent = eligible_intent(n)
        db.commit()
        assert intent.status == models.MailSendIntent.APPROVED, intent.status_reason

    # The fourth is over the ceiling, so it is NOT authorised for unattended sending.
    exhausted = eligible_intent(3)
    db.commit()
    assert exhausted.status == models.MailSendIntent.WAITING_FOR_APPROVAL
    assert "DAILY_LIMIT_REACHED" in (exhausted.status_reason or "")

    # And nothing was sent unattended beyond the ceiling.
    assert db.execute(
        select(models.MailApproval).where(
            models.MailApproval.decision == models.MailApproval.AUTONOMOUS_POLICY
        )
    ).scalars().all().__len__() == 3


def test_disabling_for_an_organisation_takes_effect_immediately(db):
    org, service = _org_and_agent(db)
    _opted_in(db, service)
    assert _policy(db, service).evaluate(platform_enabled=True, **GOOD).allowed is True

    disable_for_organisation(db, org_id=org.id)
    db.commit()

    after = _policy(db, service).evaluate(platform_enabled=True, **GOOD)
    assert after.allowed is False
    assert after.code == "AUTONOMOUS_NOT_ENABLED_FOR_ORGANISATION"


# ===========================================================================
# END TO END
# ===========================================================================
def test_an_eligible_reply_is_sent_end_to_end_without_a_person(db, platform_on):
    """The whole point of the phase: a low-risk reply goes out unattended."""
    org, service = _org_and_agent(db)
    _opportunity_and_application(db, org)
    _opted_in(db, service)
    outbound = FakeOutboundMailProvider()
    account = _mailbox(db, service)
    identity = _identity(db, service)
    draft = _draft(
        db, service,
        body="Thank you for your message. We have received your application.",
        subject="Re: your application",
    )
    svc = SendService(db, org_id=org.id, agent_id=service.get().id, outbound=outbound)

    intent = svc.create_send_intent(
        draft=draft, to_addresses=["grants@unicef.org"], from_address=identity.address,
        mail_account_id=account.id, mail_identity_id=identity.id,
        known_donor_domains=["unicef.org"],
        autonomous=True,
        classification=MailClassification.ACKNOWLEDGEMENT.value,
        thread_participants=["grants@unicef.org"],
    )
    db.commit()

    assert intent.status == models.MailSendIntent.APPROVED, intent.status_reason
    approval = db.execute(select(models.MailApproval)).scalars().one()
    assert approval.decision == models.MailApproval.AUTONOMOUS_POLICY
    assert approval.approved_by.startswith("policy:")
    assert approval.policy_evidence["gates"]

    result = svc.execute_send(intent_id=intent.id)
    db.commit()
    assert result.sent, result.detail
    assert outbound.submission_count == 1

    mail = __import__("agent.mail.service", fromlist=["GranadaMail"]).GranadaMail(
        db, org_id=org.id, agent_id=service.get().id
    )
    assert mail.status()["emails_sent_today"] == 1


def test_a_policy_downgrade_between_creation_and_send_blocks_it(db, platform_on):
    """The authorisation is re-evaluated from LIVE state, not trusted.

    A decision taken when the intent was created is a statement about the world then.
    An organisation that has since opted out, or an agent that has been paused, must
    stop an unattended send that was authorised minutes earlier.
    """
    org, service = _org_and_agent(db)
    _opportunity_and_application(db, org)
    _opted_in(db, service)
    outbound = FakeOutboundMailProvider()
    account = _mailbox(db, service)
    identity = _identity(db, service)
    draft = _draft(
        db, service,
        body="Thank you for your message. We have received your application.",
        subject="Re: your application",
    )
    svc = SendService(db, org_id=org.id, agent_id=service.get().id, outbound=outbound)

    intent = svc.create_send_intent(
        draft=draft, to_addresses=["grants@unicef.org"], from_address=identity.address,
        mail_account_id=account.id, mail_identity_id=identity.id,
        known_donor_domains=["unicef.org"], autonomous=True,
        classification=MailClassification.ACKNOWLEDGEMENT.value,
        thread_participants=["grants@unicef.org"],
    )
    db.commit()
    assert intent.status == models.MailSendIntent.APPROVED

    # The organisation changes its mind before a worker picks the job up.
    disable_for_organisation(db, org_id=org.id)
    db.commit()

    result = svc.execute_send(intent_id=intent.id)
    db.commit()
    assert result.refused, "an unattended send went out after the organisation opted out"
    assert "AUTONOMOUS_REVOKED" in (result.refusal_code or "")
    assert outbound.call_count == 0
    assert outbound.submission_count == 0


def test_an_ineligible_message_still_awaits_a_person(db, platform_on):
    """A gate failing means "not unattended", never "not sent at all"."""
    org, service = _org_and_agent(db)
    _opportunity_and_application(db, org)
    # NOT opted in.
    outbound = FakeOutboundMailProvider()
    account = _mailbox(db, service)
    identity = _identity(db, service)
    draft = _draft(
        db, service,
        body="Thank you for your message. We have received your application.",
        subject="Re: your application",
    )
    svc = SendService(db, org_id=org.id, agent_id=service.get().id, outbound=outbound)

    intent = svc.create_send_intent(
        draft=draft, to_addresses=["grants@unicef.org"], from_address=identity.address,
        mail_account_id=account.id, mail_identity_id=identity.id,
        known_donor_domains=["unicef.org"], autonomous=True,
        classification=MailClassification.ACKNOWLEDGEMENT.value,
    )
    db.commit()

    assert intent.status == models.MailSendIntent.WAITING_FOR_APPROVAL
    assert "AUTONOMOUS_NOT_ENABLED" in (intent.status_reason or "")
    assert outbound.call_count == 0

    # And a person can still approve it, so nothing was lost.
    ApprovalService(db, org_id=org.id).approve(
        intent_id=intent.id, user_id=org.owner_user_id
    )
    db.commit()
    assert svc.execute_send(intent_id=intent.id).sent
    assert outbound.submission_count == 1
