"""The verification extractor: what it must accept, and what it must refuse.

The refusals matter more than the acceptances. A code applied to the wrong account, or a link from an
unrelated message, is worse than a run that stops - so most of this file is about the cases that must
NOT complete a registration.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.mail.verification import (
    VerificationKind,
    VerificationRequest,
    extract,
    is_verification_message,
)

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def request(**over):
    base = dict(
        org_id="org-1",
        address="globalmotion@granadac.com",
        portal_host="unpartnerportal.org",
        purpose="ngo registration",
        requested_at=NOW,
        ttl=timedelta(minutes=30),
    )
    base.update(over)
    return VerificationRequest(**base)


def call(**over):
    args = dict(
        subject="Verify your UN Partner Portal account",
        body="Your verification code is 483920. It expires in 30 minutes.",
        sender="no-reply@mail.unpartnerportal.org",
        recipients=["globalmotion@granadac.com"],
        received_at=NOW + timedelta(minutes=2),
        request=request(),
    )
    args.update(over)
    return extract(**args)


# ---------------------------------------------------------------------------
# Accepted
# ---------------------------------------------------------------------------
def test_a_typical_verification_code_is_extracted():
    out = call()
    assert out.kind is VerificationKind.CODE
    assert out.code == "483920"
    assert out.usable


def test_a_code_spaced_in_two_groups_is_joined():
    out = call(body="Your code is 4839 2017 and expires shortly.")
    assert out.kind is VerificationKind.CODE
    assert out.code == "48392017"


def test_a_code_alone_on_its_own_line_is_found():
    out = call(subject="Confirm your email", body="Welcome.\n\n731244\n\nThank you.")
    assert out.kind is VerificationKind.CODE
    assert out.code == "731244"


def test_an_alphanumeric_code_is_found():
    out = call(body="Enter this code to continue: code A1B2C3")
    assert out.kind is VerificationKind.CODE
    assert out.code == "A1B2C3"


def test_a_verification_link_is_extracted_when_there_is_no_code():
    out = call(
        subject="Confirm your registration",
        body="Click to activate: https://www.unpartnerportal.org/verify?token=abc123XYZ",
    )
    assert out.kind is VerificationKind.LINK
    assert out.link == "https://www.unpartnerportal.org/verify?token=abc123XYZ"


def test_a_mailer_tracker_link_is_accepted_when_it_carries_the_token():
    out = call(
        subject="Confirm your email",
        body="Confirm: https://click.mailer.example/track?token=zz9&url=unpartnerportal.org",
    )
    assert out.kind is VerificationKind.LINK
    assert "token=zz9" in out.link


def test_footer_links_are_not_mistaken_for_the_verification_link():
    out = call(
        subject="Verify your account",
        body=(
            "Verify here: https://www.unpartnerportal.org/verify?token=real\n\n"
            "Unsubscribe: https://www.unpartnerportal.org/unsubscribe\n"
            "Privacy: https://www.unpartnerportal.org/privacy"
        ),
    )
    assert out.kind is VerificationKind.LINK
    assert "token=real" in out.link


def test_a_subdomain_sender_is_accepted():
    out = call(sender="verify@accounts.unpartnerportal.org")
    assert out.kind is VerificationKind.CODE


# ---------------------------------------------------------------------------
# Refused - these are the ones that protect the account
# ---------------------------------------------------------------------------
def test_a_code_sent_to_another_mailbox_is_refused():
    """TENANT ISOLATION. The same code arriving at a different organisation's address must not
    complete this registration."""
    out = call(recipients=["someoneelse@granadac.com"])
    assert out.kind is VerificationKind.NONE
    assert "mailbox" in out.reason


def test_a_message_arriving_before_the_request_is_refused():
    """It is a previous attempt's code. Applying it binds the wrong account."""
    out = call(received_at=NOW - timedelta(minutes=20))
    assert out.kind is VerificationKind.NONE
    assert "before" in out.reason


def test_a_message_arriving_after_expiry_is_refused():
    out = call(received_at=NOW + timedelta(minutes=45))
    assert out.kind is VerificationKind.NONE
    assert "expired" in out.reason


def test_small_clock_skew_is_tolerated():
    """Mail servers disagree by seconds; a code rejected for arriving 30s 'early' is a false
    negative the operator would have to unpick."""
    out = call(received_at=NOW - timedelta(seconds=30))
    assert out.kind is VerificationKind.CODE


def test_a_sender_impersonating_the_domain_is_refused():
    """`evil-unpartnerportal.org` ends with `unpartnerportal.org`. A naive suffix test passes it."""
    out = call(sender="no-reply@evil-unpartnerportal.org")
    assert out.kind is VerificationKind.NONE
    assert "not the portal" in out.reason


def test_an_unrelated_sender_is_refused():
    out = call(sender="newsletter@example.com")
    assert out.kind is VerificationKind.NONE


def test_a_non_verification_message_is_refused():
    out = call(subject="Your monthly newsletter", body="Here is what happened this month.")
    assert out.kind is VerificationKind.NONE


def test_a_verification_shaped_message_with_no_code_or_link_is_refused_with_a_reason():
    out = call(subject="Verify your account", body="Please verify your account soon.")
    assert out.kind is VerificationKind.NONE
    assert "no code or verification link" in out.reason


def test_a_year_in_a_verification_email_is_not_taken_as_the_code():
    """The failure mode a bare digit-run search produces."""
    out = call(subject="Verify your account", body="© 2026 UN Partner Portal. All rights reserved.")
    assert out.kind is VerificationKind.NONE


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_the_same_code_yields_the_same_fingerprint():
    """Applying one code twice must be a no-op, which is only checkable if the fingerprint is
    stable."""
    assert call().fingerprint == call().fingerprint


def test_different_codes_yield_different_fingerprints():
    assert call().fingerprint != call(body="Your code is 111111").fingerprint


def test_fingerprints_are_scoped_to_the_organisation():
    a = call(request=request(org_id="org-1"))
    b = call(request=request(org_id="org-2"))
    assert a.fingerprint != b.fingerprint


# ---------------------------------------------------------------------------
# The cheap pre-filter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "subject,body",
    [
        ("Verify your email", ""),
        ("", "Your one-time passcode is below"),
        ("Account activation", ""),
        ("", "Click to confirm your address"),
        ("", "Here is your code"),
    ],
)
def test_verification_shaped_mail_is_recognised(subject, body):
    assert is_verification_message(subject, body)


@pytest.mark.parametrize(
    "subject,body",
    [("Invoice attached", "Please find the invoice."), ("Welcome aboard", "Great to have you.")],
)
def test_ordinary_mail_is_not_verification_mail(subject, body):
    assert not is_verification_message(subject, body)
