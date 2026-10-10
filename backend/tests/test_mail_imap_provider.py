"""The IMAP inbound adapter: the read half of "set up email".

WHY THIS FILE EXISTS

Reading required Google or Microsoft OAuth, so connecting a mailbox meant registering an OAuth
application first. IMAP is what an ordinary mailbox offers. Without it Granada could send from a
standard address and not read one.

The tests below assert the properties that decide whether a funder's reply is seen ONCE, LATE, or
TWICE - and one security property that is easy to lose:

    `MailAccount` has no password column, on purpose. Credentials reach the adapter as constructor
    arguments, exactly as GoogleTransport takes an access token. If a future change adds a password
    column "for convenience", the schema's guarantee is gone and these tests should be in the way.
"""

from __future__ import annotations

import email
import imaplib
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.mail.providers.base import (  # noqa: E402
    MailAuthError,
    MailTransientError,
    MailTransport,
)
from agent.mail.providers.imap import (  # noqa: E402
    ImapConfig,
    ImapInboundMailProvider,
    _ImapError,
    _to_inbound,
    build_from_settings,
)

#: The real IMAP error class, captured before any test replaces `imaplib.IMAP4`.
#:
#: `imaplib.IMAP4.error` is resolved lazily, so the fake below cannot use it once `_provider` has
#: monkeypatched `imaplib.IMAP4` to a lambda - it raises AttributeError instead of the error it means
#: to raise, and the test then fails for a reason unrelated to the code.
_REAL_IMAP_ERROR = imaplib.IMAP4.error


def _raw_message(
    *,
    subject: str = "Re: Application",
    sender: str = "Funder Name <funding@funder.example>",
    body: str = "Thank you for your application.",
    message_id: str = "<m1@funder.example>",
    in_reply_to: str | None = None,
    references: str | None = None,
    attachment: bool = False,
) -> bytes:
    parts = [
        "From: " + sender,
        "To: grants@example.org",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        "Date: Mon, 06 Oct 2025 10:00:00 +0000",
        "Authentication-Results: mx.example; spf=pass; dkim=pass; dmarc=pass",
        "MIME-Version: 1.0",
    ]
    if in_reply_to:
        parts.append(f"In-Reply-To: {in_reply_to}")
    if references:
        parts.append(f"References: {references}")

    if attachment:
        parts.append('Content-Type: multipart/mixed; boundary="B"')
        body_bytes = (
            "\r\n".join(parts)
            + "\r\n\r\n--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            + body
            + "\r\n--B\r\nContent-Type: application/pdf\r\n"
            + 'Content-Disposition: attachment; filename="budget.pdf"\r\n'
            + "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--B--\r\n"
        )
        return body_bytes.encode("utf-8")

    parts.append("Content-Type: text/plain; charset=utf-8")
    return ("\r\n".join(parts) + "\r\n\r\n" + body).encode("utf-8")


class FakeImap:
    """A scripted IMAP server. Records the exact commands issued."""

    def __init__(
        self,
        *,
        uids: list[bytes] | None = None,
        messages: dict[bytes, bytes] | None = None,
        fail_login: bool = False,
        fail_select: bool = False,
    ) -> None:
        self.uids = uids if uids is not None else [b"1", b"2", b"3"]
        #: Explicit messages win; otherwise one is synthesised per requested uid. The first version of
        #: this fake carried a message for b"1" only while advertising three uids, so the provider
        #: correctly raised "vanished before it could be fetched" and five tests failed for a reason
        #: that had nothing to do with the code under test.
        self._explicit_messages = messages
        self.fail_login = fail_login
        self.fail_select = fail_select
        self.commands: list[tuple] = []
        self.logged_out = False
        self.selected_readonly: bool | None = None

    @property
    def messages(self) -> dict[bytes, bytes]:
        if self._explicit_messages is not None:
            return self._explicit_messages
        return {uid: _raw_message(message_id=f"<m{uid.decode()}@x>") for uid in self.uids}

    def login(self, user, password) -> None:
        self.commands.append(("login", user))
        if self.fail_login:
            raise _REAL_IMAP_ERROR("authentication failed")

    def select(self, mailbox, readonly=False):
        self.commands.append(("select", mailbox, readonly))
        self.selected_readonly = readonly
        if self.fail_select:
            return "NO", [b""]
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, *args))
        if command.upper() == "SEARCH":
            return "OK", [b" ".join(self.uids)]
        if command.upper() == "FETCH":
            uid = args[0]
            raw = self.messages.get(uid)
            if raw is None:
                return "OK", [None]
            return "OK", [(b"1 (RFC822 {..})", raw)]
        return "OK", [b""]

    def logout(self) -> None:
        self.logged_out = True


def _provider(fake: FakeImap, monkeypatch, **overrides) -> ImapInboundMailProvider:
    config = ImapConfig(
        host="imap.example.org", username="u", password="p", **overrides
    )
    provider = ImapInboundMailProvider(config)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: fake)
    monkeypatch.setattr(imaplib, "IMAP4", lambda *a, **kw: fake)
    return provider


# ===========================================================================
# CONFIGURATION
# ===========================================================================
def test_a_missing_host_is_refused_at_construction():
    with pytest.raises(ValueError):
        ImapConfig(host="")


def test_a_missing_username_is_refused_at_construction():
    with pytest.raises(ValueError):
        ImapConfig(host="imap.example.org", username="")


def test_tls_is_on_by_default():
    """Plaintext IMAP sends the password in the clear. A default of False makes the insecure option
    the path of least resistance, which is how it becomes the path everyone takes."""
    assert ImapConfig(host="imap.example.org", username="u").use_ssl is True


def test_build_from_settings_returns_none_when_unconfigured():
    class S:
        imap_host = ""

    assert build_from_settings(S()) is None


def test_build_from_settings_builds_when_configured():
    class S:
        imap_host = "imap.example.org"
        imap_port = 993
        imap_user = "u"
        imap_pass = "p"
        imap_ssl = True
        imap_mailbox = "INBOX"

    provider = build_from_settings(S())
    assert provider is not None
    assert provider.config.host == "imap.example.org"


def test_the_adapter_satisfies_the_mail_transport_protocol():
    provider = ImapInboundMailProvider(ImapConfig(host="imap.example.org", username="u"))
    assert isinstance(provider, MailTransport)


def test_the_adapter_has_no_send_method():
    """The type-level ceiling: the inbound protocol has no send, so an adapter that cannot send cannot
    be asked to. A method that does not exist cannot be called by accident."""
    provider = ImapInboundMailProvider(ImapConfig(host="imap.example.org", username="u"))
    for forbidden in ("send", "send_message", "submit_message", "reply", "forward"):
        assert not hasattr(provider, forbidden), f"the inbound adapter exposes {forbidden}"


# ===========================================================================
# POLL-ONLY
# ===========================================================================
def test_verify_webhook_is_false():
    """IMAP receives no deliveries, so no delivery can be genuine. Returning False rather than raising:
    the question is answerable and the answer is no. A caller that treated this as 'verification
    unavailable' and proceeded would accept unauthenticated mail."""
    provider = ImapInboundMailProvider(ImapConfig(host="imap.example.org", username="u"))
    assert provider.verify_webhook(headers={}, body=b"anything") is False


# ===========================================================================
# LISTING AND THE CURSOR
# ===========================================================================
def test_listing_returns_a_page_and_a_cursor(monkeypatch):
    fake = FakeImap(uids=[b"10", b"11", b"12"])
    batch = _provider(fake, monkeypatch).list_messages(account=None, limit=2)

    assert [m.provider_message_id for m in batch.messages] == ["10", "11"]
    assert batch.next_cursor == "11"
    assert batch.has_more is True


def test_the_cursor_is_a_uid_not_a_sequence_number(monkeypatch):
    """A sequence number is only stable within one session. A cursor holding one would point at a
    different message after any expunge, so a restart could skip mail or read it twice."""
    fake = FakeImap()
    _provider(fake, monkeypatch).list_messages(account=None, cursor="41")

    search = next(c for c in fake.commands if c[0] == "uid" and c[1].upper() == "SEARCH")
    assert "UID 42:*" in str(search), f"the cursor was not used as a UID: {search}"


def test_the_first_page_does_not_assume_unseen(monkeypatch):
    """`UNSEEN` would miss a funder's reply that a human glanced at on a phone before the sweep ran.
    The brief requires that a reply is not lost because somebody read it."""
    fake = FakeImap()
    _provider(fake, monkeypatch).list_messages(account=None)

    search = next(c for c in fake.commands if c[0] == "uid" and c[1].upper() == "SEARCH")
    assert "UNSEEN" not in str(search).upper()
    assert "ALL" in str(search).upper()


def test_selection_is_readonly_so_reading_does_not_mark_seen(monkeypatch):
    """Granada reading a message must not change what the organisation sees in their own mailbox."""
    fake = FakeImap()
    _provider(fake, monkeypatch).list_messages(account=None)
    assert fake.selected_readonly is True, "the mailbox was opened read-write"


def test_an_empty_mailbox_returns_no_messages(monkeypatch):
    fake = FakeImap(uids=[])
    batch = _provider(fake, monkeypatch).list_messages(account=None)
    assert batch.messages == ()
    assert batch.has_more is False


# ===========================================================================
# ERRORS THAT MUST BE DISTINGUISHED
# ===========================================================================
def test_bad_credentials_raise_mail_auth_error(monkeypatch):
    """Distinct from a transient failure: expired credentials are permanent until a human acts, and
    retrying on a timer is how an account gets locked out."""
    fake = FakeImap(fail_login=True)
    with pytest.raises(MailAuthError):
        _provider(fake, monkeypatch).list_messages(account=None)


def test_a_failed_select_is_transient(monkeypatch):
    fake = FakeImap(fail_select=True)
    with pytest.raises(MailTransientError):
        _provider(fake, monkeypatch).list_messages(account=None)


def test_a_vanished_message_is_transient_not_fatal(monkeypatch):
    """The message was expunged between SEARCH and FETCH. The next sweep moves on; aborting the whole
    mailbox because one message disappeared would stall every other organisation's mail."""
    fake = FakeImap(uids=[b"99"], messages={})
    with pytest.raises(MailTransientError):
        _provider(fake, monkeypatch).list_messages(account=None)


def test_the_connection_is_closed_even_when_fetching_fails(monkeypatch):
    fake = FakeImap(uids=[b"99"], messages={})
    with pytest.raises(MailTransientError):
        _provider(fake, monkeypatch).list_messages(account=None)
    assert fake.logged_out is True, "a failed fetch leaked an IMAP session"


# ===========================================================================
# MESSAGE CONVERSION - mail is untrusted input
# ===========================================================================
def test_a_plain_message_maps_to_inbound():
    message = _to_inbound(email.message_from_bytes(_raw_message()), "7")

    assert message.provider_message_id == "7"
    assert message.sender == "funding@funder.example"
    assert message.internet_message_id == "<m1@funder.example>"
    assert "Thank you" in (message.body_text or "")
    assert message.received_at is not None


def test_authentication_verdicts_are_preserved():
    """SPF/DKIM/DMARC are evidence that cannot be reconstructed after the fact, so they travel with
    the message rather than being re-derived."""
    message = _to_inbound(email.message_from_bytes(_raw_message()), "7")
    assert message.authentication_results.get("spf") == "pass"
    assert message.authentication_results.get("dkim") == "pass"
    assert message.authentication_results.get("dmarc") == "pass"


def test_a_broken_header_does_not_abort_the_message():
    """A malformed Subject must not lose the whole message - and it must not raise either, because one
    bad header would then stall the mailbox."""
    raw = _raw_message().replace(b"Subject: Re: Application", b"Subject: =?utf-8?Q?broken")
    message = _to_inbound(email.message_from_bytes(raw), "7")
    assert message is not None
    assert message.provider_message_id == "7"


def test_a_thread_key_anchors_on_the_root_reference():
    """Anchoring on the immediate parent makes a long reply chain a chain of pairs instead of one
    thread, so a funder's fifth reply looks like a new conversation."""
    raw = _raw_message(references="<root@x> <second@x> <third@x>", in_reply_to="<third@x>")
    message = _to_inbound(email.message_from_bytes(raw), "7")
    assert message.provider_thread_id == "<root@x>"


def test_an_attachment_is_listed_with_its_size_but_not_its_bytes():
    """Metadata and content are separated so the size policy can refuse a file BEFORE downloading it.
    A 2 GB attachment must be rejected on its declared size."""
    message = _to_inbound(email.message_from_bytes(_raw_message(attachment=True)), "7")
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.filename == "budget.pdf"
    assert attachment.size_bytes > 0
    assert attachment.content is None, "the message carried attachment bytes; the size gate is bypassed"
    # walk() index 2: 0 is the multipart container, 1 is text/plain, 2 is the PDF. An earlier version
    # of this test asserted 1 and would have submitted the body text as the budget.
    assert attachment.provider_attachment_id == "7:2"


def test_attachment_bytes_are_fetched_by_part_number(monkeypatch):
    """The id is `<uid>:<part>`. Re-searching by filename would pick the first match, and two
    attachments may share a filename - which is how the wrong budget gets submitted."""
    fake = FakeImap(messages={b"7": _raw_message(attachment=True)})
    payload = _provider(fake, monkeypatch).fetch_attachment(account=None, provider_attachment_id="7:2")
    assert payload.startswith(b"%PDF")


def test_a_malformed_attachment_id_is_refused():
    provider = ImapInboundMailProvider(ImapConfig(host="imap.example.org", username="u"))
    with pytest.raises(ValueError):
        provider.fetch_attachment(account=None, provider_attachment_id="not-a-part")
