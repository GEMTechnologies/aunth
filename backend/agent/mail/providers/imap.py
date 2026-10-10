"""Inbound mail over IMAP.

WHY THIS EXISTS

Reading required Google or Microsoft OAuth. Both need a consent screen and an app registration before a
single message can be fetched, so "connect a mailbox" meant "register an OAuth application first". IMAP
is what an ordinary mailbox offers, and without it Granada can send from a standard address but cannot
read one.

THE SCHEMA FORBIDS A STORED PASSWORD, AND THIS ADAPTER OBEYS IT

`MailAccount` has no password column, on purpose:

    "No provider password is ever stored. There is no column for one, and `credentials_ref` points at
     the secret store rather than holding a secret. That is the security gate's rule and it is enforced
     by the schema's shape: a password has nowhere to go."

So credentials arrive here as CONSTRUCTOR ARGUMENTS, exactly as `GoogleTransport` takes `access_token`.
The caller resolves them from wherever secrets live. This module never reads a database, never writes a
credential, and has no field to leak one into. `credentials_ref` is the caller's problem, and that is
the right place for it.

POLL-ONLY, AND `verify_webhook` SAYS SO

IMAP has no webhooks. `verify_webhook` returns **False** rather than raising, because the interface's
question is "did this delivery genuinely come from the provider" and the honest answer for a transport
that receives no deliveries is no. A caller that trusted a True here would accept anything.

A `False` also means the account must be polled, which is a real property: the fleet's reconciliation
sweep is what finds new mail for an IMAP mailbox, and nothing pushes.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from agent.mail.providers.base import (
    InboundAttachment,
    InboundMessage,
    MailAuthError,
    MailTransientError,
    SyncBatch,
)

logger = logging.getLogger(__name__)

#: IMAP's own exception class, ALIASED AT MODULE SCOPE.
#:
#: `except imaplib.IMAP4.error` resolves the attribute at the moment the exception is handled, which
#: means anything that replaces `imaplib.IMAP4` - a test double, a monkeypatch, a future shim - turns
#: error handling into `AttributeError: 'function' object has no attribute 'error'`. The failure then
#: masks the real one: an authentication error is reported as a missing attribute.
#:
#: Found by a test that patched the class to script a login failure. Binding the exception once, here,
#: makes error handling independent of how the client class is supplied.
_ImapError = imaplib.IMAP4.error

#: How many messages one page may hold. Bounded like every other sweep in this codebase: a mailbox with
#: fifty thousand messages must not be pulled into one transaction.
DEFAULT_PAGE_SIZE = 50

#: IMAP search criteria that mean "everything the server will show us". `UNSEEN` is deliberately NOT
#: the default: a message read on a phone before the sweep runs would never be seen by Granada, and the
#: brief requires that a funder's reply is not missed because a human glanced at it.
DEFAULT_CRITERIA = "ALL"


@dataclass(frozen=True)
class ImapConfig:
    """Everything needed to reach one mailbox over IMAP.

    `use_ssl` defaults to True because IMAP over plaintext sends the password in the clear. A default of
    False would make an insecure connection the path of least resistance, which is how it becomes the
    path everyone takes.
    """

    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    mailbox: str = "INBOX"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("imap host is required")
        if not self.username:
            raise ValueError("imap username is required")


class ImapInboundMailProvider:
    """Read a mailbox over IMAP.

    A connection per call. A pooled connection would be faster and would also mean a worker that died
    mid-fetch leaves a session in an unknown state for the next worker to reuse; for a funder's reply,
    one connect per operation is the cheaper mistake.

    `capabilities` is deliberately absent: the inbound interface has no send method at all, so there is
    nothing here for a capability check to gate. That is the type-level ceiling working as intended.
    """

    name = "IMAP"

    def __init__(self, config: ImapConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # The MailTransport interface
    # ------------------------------------------------------------------
    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> bool:
        """False, always. IMAP receives no deliveries, so no delivery can be genuine.

        Returning False rather than raising: the interface's question is answerable, and the answer is
        no. A caller that treated this as "verification unavailable" and proceeded would be accepting
        unauthenticated mail on an endpoint this transport does not even have.
        """
        return False

    def list_messages(
        self, *, account: Any, cursor: Optional[str] = None, limit: int = DEFAULT_PAGE_SIZE
    ) -> SyncBatch:
        """A page of messages, oldest first, resuming after `cursor`.

        THE CURSOR IS A UID, and UIDs are used rather than sequence numbers because sequence numbers are
        only stable within a session. A cursor holding a sequence number would point at a different
        message after any expunge, so a restart could skip mail or read it twice.
        """
        del account  # credentials live on this adapter, not on the row
        client = self._connect()
        try:
            self._select(client)
            since = f"UID {int(cursor) + 1}:*" if cursor else DEFAULT_CRITERIA
            status, data = client.uid("SEARCH", None, since)
            if status != "OK":
                raise MailTransientError(f"IMAP SEARCH failed: {status}")

            uids = [u for u in (data[0] or b"").split() if u]
            page = uids[: max(1, int(limit))]
            if not page:
                return SyncBatch(messages=(), next_cursor=cursor, has_more=False)

            messages = tuple(self._fetch(client, uid) for uid in page)
            last = page[-1].decode("ascii", errors="replace")
            return SyncBatch(
                messages=messages,
                next_cursor=last,
                has_more=len(uids) > len(page),
            )
        finally:
            self._close(client)

    def fetch_message(self, *, account: Any, provider_message_id: str) -> InboundMessage:
        """Retrieve one message by UID. Raises rather than returning a partial message."""
        del account
        client = self._connect()
        try:
            self._select(client)
            return self._fetch(client, provider_message_id.encode("ascii"))
        finally:
            self._close(client)

    def fetch_attachment(self, *, account: Any, provider_attachment_id: str) -> bytes:
        """Retrieve attachment bytes, after the size policy has approved it.

        The id is `<uid>:<part-number>`, so one round trip fetches the message and the part is selected
        locally. Storing the part number rather than re-searching by filename matters: two attachments
        may share a filename, and picking the first is how the wrong budget gets submitted.
        """
        del account
        uid, _, part_number = provider_attachment_id.partition(":")
        if not uid or not part_number:
            raise ValueError(f"attachment id must be '<uid>:<part>', got {provider_attachment_id!r}")

        client = self._connect()
        try:
            self._select(client)
            message = self._load(client, uid.encode("ascii"))
        finally:
            self._close(client)

        target = int(part_number)
        for index, part in enumerate(message.walk()):
            if index != target:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                raise MailTransientError(f"attachment part {part_number} had no decodable payload")
            return payload
        raise MailTransientError(f"attachment part {part_number} is not present in message {uid}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _connect(self) -> imaplib.IMAP4:
        config = self.config
        try:
            if config.use_ssl:
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    config.host, config.port, timeout=config.timeout_seconds
                )
            else:
                client = imaplib.IMAP4(config.host, config.port, timeout=config.timeout_seconds)
        except (OSError, _ImapError) as exc:
            # The transport itself failed. Transient by classification: retrying later may work.
            raise MailTransientError(f"could not reach the IMAP server: {type(exc).__name__}") from exc

        try:
            client.login(config.username, config.password)
        except _ImapError as exc:
            # Distinct from every other failure. Expired or revoked credentials are permanent until a
            # human acts, and retrying on a timer is how an account gets locked out.
            self._close(client)
            raise MailAuthError("the IMAP server rejected the credentials") from exc
        return client

    def _select(self, client: imaplib.IMAP4) -> None:
        try:
            status, _ = client.select(self.config.mailbox, readonly=True)
        except _ImapError as exc:
            raise MailTransientError(f"could not select {self.config.mailbox}: {exc}") from exc
        if status != "OK":
            raise MailTransientError(f"could not select {self.config.mailbox}: {status}")

    def _fetch(self, client: imaplib.IMAP4, uid: bytes) -> InboundMessage:
        return _to_inbound(self._load(client, uid), uid.decode("ascii", errors="replace"))

    def _load(self, client: imaplib.IMAP4, uid: bytes) -> Message:
        """Fetch and parse raw RFC822. `readonly=True` on SELECT already guarantees no \\Seen flag."""
        try:
            status, data = client.uid("FETCH", uid, "(RFC822)")
        except _ImapError as exc:
            raise MailTransientError(f"IMAP FETCH failed: {exc}") from exc
        if status != "OK" or not data:
            raise MailTransientError(f"IMAP FETCH returned {status} for uid {uid!r}")

        raw = next(
            (
                item[1]
                for item in data
                if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray))
            ),
            None,
        )
        if raw is None:
            # The message was expunged between SEARCH and FETCH. Transient: the next sweep moves on.
            raise MailTransientError(f"uid {uid!r} vanished before it could be fetched")
        return email.message_from_bytes(bytes(raw))

    def _close(self, client: imaplib.IMAP4) -> None:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - the session is being discarded either way
            pass


# ---------------------------------------------------------------------------
# RFC822 -> Granada's vocabulary
# ---------------------------------------------------------------------------
def _to_inbound(message: Message, uid: str) -> InboundMessage:
    """Convert a parsed message. Every field is defensive: mail is untrusted input.

    A malformed header must not abort a sweep for the whole mailbox, so a header that cannot be decoded
    falls back to its raw form rather than raising. The one thing that IS required is the UID, because
    without it Granada cannot name the message it just read.
    """
    attachments: list[InboundAttachment] = []
    body_text: Optional[str] = None
    body_html: Optional[str] = None

    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        content_type = (part.get_content_type() or "").lower()
        if "attachment" in disposition or (
            part.get_filename() and content_type not in ("text/plain", "text/html")
        ):
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                InboundAttachment(
                    provider_attachment_id=f"{uid}:{index}",
                    filename=_header(part.get_filename()) if part.get_filename() else None,
                    mime_type=part.get_content_type(),
                    size_bytes=len(payload),
                )
            )
            continue
        if content_type == "text/plain" and body_text is None:
            body_text = _body(part)
        elif content_type == "text/html" and body_html is None:
            body_html = _body(part)

    received_at: Optional[datetime] = None
    if message.get("Date"):
        try:
            parsed = parsedate_to_datetime(message["Date"])
            # A naive timestamp is treated as UTC rather than local: the server's timezone is not
            # Granada's, and guessing shifts every message by an unknown amount.
            received_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            received_at = None

    return InboundMessage(
        provider_message_id=uid,
        provider_thread_id=_thread_id(message),
        internet_message_id=message.get("Message-ID"),
        in_reply_to=message.get("In-Reply-To"),
        references=tuple((message.get("References") or "").split()),
        sender=_address(message.get("From")),
        sender_name=_header(message.get("From")),
        recipients=tuple(_addresses(message.get("To"))),
        subject=_header(message.get("Subject")),
        body_text=body_text,
        body_html=body_html,
        received_at=received_at,
        # The delivered authentication verdicts, kept because they cannot be reconstructed later.
        authentication_results=_auth_results(message),
        attachments=tuple(attachments),
        headers={k: _header(v) for k, v in message.items()},
    )


def _header(value: Optional[str]) -> Optional[str]:
    """Decode a possibly-encoded header without raising on malformed input."""
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a broken header is data, not a crash
        return value


def _body(part: Message) -> Optional[str]:
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # An unknown charset is the sender's problem; guessing utf-8 would mangle less than refusing.
        return payload.decode("utf-8", errors="replace")


_ADDRESS_RE = re.compile(r"<([^>]+)>")


def _address(value: Optional[str]) -> Optional[str]:
    decoded = _header(value)
    if not decoded:
        return None
    match = _ADDRESS_RE.search(decoded)
    return (match.group(1) if match else decoded).strip() or None


def _addresses(value: Optional[str]) -> list[str]:
    decoded = _header(value)
    if not decoded:
        return []
    found = _ADDRESS_RE.findall(decoded)
    if found:
        return [a.strip() for a in found if a.strip()]
    return [a.strip() for a in decoded.split(",") if a.strip()]


def _auth_results(message: Message) -> dict[str, Any]:
    """SPF/DKIM/DMARC verdicts, when the receiving server recorded them."""
    raw = message.get("Authentication-Results") or message.get("ARC-Authentication-Results")
    if not raw:
        return {}
    text = _header(raw) or ""
    results: dict[str, Any] = {"raw_present": True}
    for mechanism in ("spf", "dkim", "dmarc"):
        match = re.search(rf"{mechanism}\s*=\s*(\w+)", text, re.IGNORECASE)
        if match:
            results[mechanism] = match.group(1).lower()
    return results


def _thread_id(message: Message) -> Optional[str]:
    """A thread key: the root of the References chain, else In-Reply-To, else the Message-ID.

    Anchoring on the ROOT rather than the immediate parent is what makes a long reply chain one thread
    instead of a chain of pairs.
    """
    references = (message.get("References") or "").split()
    if references:
        return references[0]
    if message.get("In-Reply-To"):
        return (message.get("In-Reply-To") or "").split()[0] or None
    return message.get("Message-ID")


def build_from_settings(settings: Any) -> Optional[ImapInboundMailProvider]:
    """Construct from settings, or None when IMAP is not configured.

    None rather than raising: an unconfigured transport is a deployment state, and the caller should
    park the work rather than fail the job and burn a retry budget.
    """
    host = getattr(settings, "imap_host", "") or ""
    if not host:
        return None
    return ImapInboundMailProvider(
        ImapConfig(
            host=host,
            port=int(getattr(settings, "imap_port", 993) or 993),
            username=getattr(settings, "imap_user", "") or "",
            password=getattr(settings, "imap_pass", "") or "",
            use_ssl=bool(getattr(settings, "imap_ssl", True)),
            mailbox=getattr(settings, "imap_mailbox", "INBOX") or "INBOX",
        )
    )
