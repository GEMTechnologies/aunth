"""The SMTP outbound adapter: what "set up email" means for an ordinary mailbox.

WHY THIS FILE EXISTS

Granada shipped Google and Microsoft Graph adapters - both OAuth, both requiring a consent screen -
while `config.py` declared `smtp_host`, `smtp_port`, `smtp_user`, `smtp_pass`, `smtp_tls` and
`smtp_ssl` and **no code read any of them**. Dead configuration is worse than absent configuration: a
deployment sets it, sees it accepted, and believes mail is wired.

These tests are written against a FAKE SMTP SERVER rather than a real one, and they assert the things
that decide whether a donor receives one email or two:

  * a pre-data rejection is CONFIRMED_NOT_SENT (a retry is safe)
  * a post-data failure is DELIVERY_UNKNOWN (a retry is FORBIDDEN)
  * `query_submission` never claims authoritative absence, because SMTP cannot prove a negative

The middle one is the whole reason this adapter is written against `mail`/`rcpt`/`data` instead of
`smtplib.sendmail()`, which collapses every failure into one exception and leaves the caller unable to
tell "refused" from "possibly delivered".
"""

from __future__ import annotations

import smtplib
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.mail.outbound import (  # noqa: E402
    DEFINITE_NOT_SENT,
    INDETERMINATE,
    OutboundMessage,
    SendFailure,
    SendOutcome,
)
from agent.mail.providers.smtp import (  # noqa: E402
    SEND_CAPABLE,
    SmtpConfig,
    SmtpOutboundMailProvider,
    _failure_for_code,
    _summary_for,
    build_from_settings,
)


def _message() -> OutboundMessage:
    return OutboundMessage(
        from_address="grants@example.org",
        to_addresses=("funding@funder.example",),
        subject="Application: clean water programme",
        body_text="Please find our application attached.",
        attachments=(
            {
                "filename": "budget.pdf",
                "mime_type": "application/pdf",
                "content": b"%PDF-1.4 fake",
                "checksum_sha256": "abc",
            },
        ),
        approval_fingerprint="fp-123",
        internet_message_id="<fixed@example.org>",
    )


class FakeSmtp:
    """A scripted SMTP conversation.

    Records the order of calls so a test can assert that a pre-data failure never reached `data()`. The
    ORDER is the assertion: a failure classified as "nothing sent" is only correct if `data` was in fact
    never reached.
    """

    def __init__(
        self,
        *,
        fail_at: str = "",
        exc: Exception | None = None,
        code: int = 550,
    ) -> None:
        self.calls: list[str] = []
        self.payload: bytes | None = None
        self.fail_at = fail_at
        self.exc = exc
        self.code = code
        self.quit_called = False

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_at != stage:
            return
        if self.exc is not None:
            raise self.exc
        raise smtplib.SMTPResponseException(self.code, b"scripted failure")

    def ehlo(self) -> None:
        self.calls.append("ehlo")
        self._maybe_fail("ehlo")

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")
        self._maybe_fail("starttls")

    def login(self, user, password) -> None:
        self.calls.append("login")
        self._maybe_fail("login")

    def mail(self, sender) -> None:
        self.calls.append("mail")
        self._maybe_fail("mail")

    def rcpt(self, address) -> None:
        self.calls.append("rcpt")
        self._maybe_fail("rcpt")

    def data(self, payload) -> None:
        self.calls.append("data")
        self.payload = payload
        self._maybe_fail("data")

    def quit(self) -> None:
        self.calls.append("quit")
        self.quit_called = True

    def close(self) -> None:
        pass


def _provider_with(fake: FakeSmtp, monkeypatch=None, **config_kwargs) -> SmtpOutboundMailProvider:
    """Build a provider whose SMTP class IS the fake.

    `smtplib.SMTP` is patched rather than `provider._connect`. An earlier version replaced `_connect`
    outright, which meant `ehlo` and `starttls` never ran - so three tests asserted behaviour on a code
    path they had silently skipped, and passed or failed for the wrong reason.
    """
    config = SmtpConfig(host="smtp.example.org", port=587, **config_kwargs)
    provider = SmtpOutboundMailProvider(config)
    if monkeypatch is not None:
        monkeypatch.setattr(smtplib, "SMTP", lambda *a, **kw: fake)
        monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: fake)
    else:
        provider._connect = lambda: fake  # type: ignore[method-assign]
    return provider


# ===========================================================================
# CONFIGURATION
# ===========================================================================
def test_a_missing_host_is_refused_at_construction_not_at_first_send():
    """An empty host would surface as a failed send at 3am. Refusing here makes it a startup error."""
    with pytest.raises(ValueError):
        SmtpConfig(host="")


def test_ssl_and_starttls_together_are_refused():
    """Not 'more secure' - a startup error on every real server, because the session is already
    encrypted and STARTTLS is then refused or silently ignored depending on the server."""
    with pytest.raises(ValueError):
        SmtpConfig(host="smtp.example.org", use_ssl=True, use_starttls=True)


def test_a_username_without_a_password_is_refused():
    """Half a credential is a configuration mistake, and it fails much later without this."""
    with pytest.raises(ValueError):
        SmtpConfig(host="smtp.example.org", username="user", password="")


def test_smtp_does_not_advertise_reconciliation():
    """`MAIL_RECONCILE_SEND` is absent on purpose. Advertising it would let the pipeline ask a question
    this adapter is guaranteed to answer with 'unknown'."""
    assert "MAIL_RECONCILE_SEND" not in SEND_CAPABLE
    assert "MAIL_SEND" in SEND_CAPABLE


def test_build_from_settings_returns_none_when_unconfigured():
    """The declared default host is `localhost`, which means unset. An unconfigured transport is a
    deployment state, and the caller should park the work rather than fail the job."""

    class S:
        smtp_host = "localhost"

    assert build_from_settings(S()) is None


def test_build_from_settings_builds_when_configured():
    class S:
        smtp_host = "smtp.example.org"
        smtp_port = 465
        smtp_user = "u"
        smtp_pass = "p"
        smtp_tls = False
        smtp_ssl = True

    provider = build_from_settings(S())
    assert provider is not None
    assert provider.config.host == "smtp.example.org"
    assert provider.config.use_ssl is True


# ===========================================================================
# THE SUCCESS PATH
# ===========================================================================
def test_a_successful_send_is_confirmed_with_the_message_id(monkeypatch):
    fake = FakeSmtp()
    result = _provider_with(fake, monkeypatch).submit_message(
        message=_message(), idempotency_key="k1"
    )

    assert result.outcome == SendOutcome.CONFIRMED_SENT
    assert result.internet_message_id == "<fixed@example.org>"
    assert result.is_definite
    assert not result.may_retry_now
    assert fake.calls[:2] == ["ehlo", "starttls"]
    assert "data" in fake.calls


def test_the_approval_fingerprint_travels_with_the_message():
    """The sent copy is self-describing about what was authorised, so 'the provider sent what was
    approved' is checkable from the recipient's own copy."""
    fake = FakeSmtp()
    _provider_with(fake).submit_message(message=_message(), idempotency_key="k1")
    assert b"X-Granada-Approval-Fingerprint: fp-123" in fake.payload


def test_attachments_are_attached():
    fake = FakeSmtp()
    _provider_with(fake).submit_message(message=_message(), idempotency_key="k1")
    assert b"budget.pdf" in fake.payload


# ===========================================================================
# THE THREE FAILURE ZONES - the reason this adapter exists
# ===========================================================================
@pytest.mark.parametrize("stage", ["ehlo", "mail", "rcpt"])
def test_a_pre_data_failure_is_definitely_not_sent(stage, monkeypatch):
    """Zones 1 and 2. The server never accepted the body, so a retry cannot duplicate anything."""
    fake = FakeSmtp(fail_at=stage, code=550)
    result = _provider_with(fake, monkeypatch).submit_message(
        message=_message(), idempotency_key="k1"
    )

    assert result.outcome == SendOutcome.CONFIRMED_NOT_SENT, f"{stage} was not classified as definite"
    assert result.failure in DEFINITE_NOT_SENT
    assert "data" not in fake.calls, "the body was sent despite a pre-data failure"


def test_a_transient_pre_data_failure_is_retryable():
    fake = FakeSmtp(fail_at="rcpt", code=451)
    result = _provider_with(fake).submit_message(message=_message(), idempotency_key="k1")
    assert result.outcome == SendOutcome.CONFIRMED_NOT_SENT
    assert result.failure == SendFailure.TEMPORARY_FAILURE
    assert result.may_retry_now is True


def test_a_failure_after_the_body_is_delivery_unknown():
    """ZONE 3, AND THE ONE THAT MATTERS MOST.

    The terminator was transmitted and the response was lost. The server MAY have the message. Granada
    must not be able to retry, because a retry here is a duplicate donor email.
    """
    fake = FakeSmtp(fail_at="data", exc=ConnectionResetError("connection reset"))
    result = _provider_with(fake).submit_message(message=_message(), idempotency_key="k1")

    assert result.outcome == SendOutcome.DELIVERY_UNKNOWN
    assert result.failure in INDETERMINATE
    assert result.is_definite is False
    assert result.may_retry_now is False, (
        "a post-data failure permitted a retry; that is how a funder receives two applications"
    )
    assert "data" in fake.calls, "the test did not actually reach the ambiguous zone"


def test_an_authentication_failure_is_not_retried_on_a_timer(monkeypatch):
    """Bad credentials are permanent until a human acts. Retrying an expired credential is how an
    account gets locked out."""
    fake = FakeSmtp(fail_at="login", exc=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    provider = _provider_with(fake, monkeypatch, username="u", password="p")
    result = provider.submit_message(message=_message(), idempotency_key="k1")

    assert result.outcome == SendOutcome.CONFIRMED_NOT_SENT
    assert result.failure == SendFailure.AUTH_REQUIRED
    assert result.may_retry_now is False


def test_an_unreachable_server_is_definitely_not_sent_and_retryable(monkeypatch):
    fake = FakeSmtp(fail_at="ehlo", exc=ConnectionRefusedError("refused"))
    result = _provider_with(fake, monkeypatch).submit_message(
        message=_message(), idempotency_key="k1"
    )

    assert result.outcome == SendOutcome.CONFIRMED_NOT_SENT
    assert result.may_retry_now is True


# ===========================================================================
# RECONCILIATION - honest ignorance
# ===========================================================================
def test_reconciliation_never_claims_authoritative_absence():
    """SMTP keeps no send log a client can read, so it cannot prove a message was NOT delivered.
    Claiming otherwise would license exactly the duplicate this module is shaped to prevent."""
    provider = _provider_with(FakeSmtp())
    result = provider.query_submission(granada_message_ref="ref-1")

    assert result.found is False
    assert result.authoritative_absence is False, (
        "the adapter claimed to know the message was not sent, which SMTP cannot establish"
    )


# ===========================================================================
# REPLY-CODE MAPPING - and the invented-enum defect that this test exists to catch
# ===========================================================================
def test_every_mapped_failure_is_a_real_member_and_is_definite():
    """The first draft of `_failure_for_code` invented five plausible members that do not exist:
    PERMANENT_FAILURE, AUTHENTICATION_FAILED, RECIPIENT_REJECTED, MESSAGE_REJECTED and
    PROVIDER_UNAVAILABLE. This walks every branch, so an invented name raises AttributeError here
    rather than at 3am in production."""
    for code in (421, 450, 451, 452, 550, 551, 552, 553, 554, 555, 500, 599):
        failure = _failure_for_code(code)
        assert isinstance(failure, SendFailure), f"code {code} mapped to {failure!r}"
        assert failure in DEFINITE_NOT_SENT, (
            f"code {code} mapped to {failure}, which is NOT in DEFINITE_NOT_SENT - a pre-data "
            f"failure must never be classified as indeterminate"
        )
        assert _summary_for(failure)


def test_recipient_rejection_maps_to_invalid_message():
    """550 is a bad mailbox. Granada's vocabulary calls that INVALID_MESSAGE - there is no
    RECIPIENT_REJECTED member, however natural the name reads."""
    assert _failure_for_code(550) == SendFailure.INVALID_MESSAGE


def test_policy_refusal_maps_to_policy_block():
    assert _failure_for_code(554) == SendFailure.PROVIDER_POLICY_BLOCK


def test_the_summary_never_echoes_the_server_text():
    """A server message can quote the recipient or the body, and this string is persisted and read far
    more casually than message content."""
    summary = _summary_for(SendFailure.INVALID_MESSAGE)
    assert "@" not in summary
    assert "funding@funder.example" not in summary


# ===========================================================================
# HEADER SAFETY
# ===========================================================================
def test_a_header_may_not_override_a_recipient():
    """Silently replacing `To` is how a message reaches the wrong funder. Refusing is the safe half of
    'the provider sent what was approved'."""
    provider = _provider_with(FakeSmtp())
    message = _message()
    message.headers = {"To": "attacker@example.invalid"}
    with pytest.raises(ValueError):
        provider._build(message)
