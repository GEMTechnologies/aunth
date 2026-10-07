"""Provider-neutral mail transport.

The business logic never learns which vendor carried a message. Everything above
this module works in terms of `InboundMessage`, `ProviderEvent` and
`MailTransport`, so adding a provider is a new class here and nothing else.

**Sending is absent, by design.** `MailTransport` has no ``send`` method at all -
not one that raises, not one that returns early. A method that exists can be
called by accident; a method that does not exist cannot. `ExternalActionDisabled`
and a capability check are still provided, because the *policy* layer must be able
to name and record the refusal, and because Phase 7b will add the method and the
refusal must not be the only thing standing between a draft and a funder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable

from agent.mail.vocabulary import Capability, assert_capability


@dataclass(frozen=True)
class InboundMessage:
    """One message as the provider describes it, before Granada interprets it.

    Deliberately close to the wire format and free of Granada concepts. It carries
    the authentication verdicts the provider exposed and the raw provider payload
    reference, because both are evidence that cannot be reconstructed later.
    """

    provider_message_id: str
    provider_thread_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: tuple[str, ...] = ()
    sender: Optional[str] = None
    sender_name: Optional[str] = None
    recipients: tuple[str, ...] = ()
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    received_at: Optional[datetime] = None
    authentication_results: dict[str, Any] = field(default_factory=dict)
    attachments: tuple["InboundAttachment", ...] = ()
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundAttachment:
    """Attachment metadata. The bytes are fetched separately, on purpose.

    Splitting metadata from content is what lets the size policy refuse a file
    *before* downloading it. A 2 GB attachment should be rejected on its declared
    size, not after it has been pulled into memory.
    """

    provider_attachment_id: str
    filename: Optional[str]
    mime_type: Optional[str]
    size_bytes: int
    content: Optional[bytes] = None


@dataclass(frozen=True)
class ProviderEvent:
    """A webhook or sync notification, before Granada has resolved the tenant."""

    provider_event_id: str
    event_type: str
    provider_account_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    provider_thread_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None


@dataclass(frozen=True)
class SyncBatch:
    """A page of messages from a reconciliation or full-sync call."""

    messages: tuple[InboundMessage, ...]
    next_cursor: Optional[str]
    has_more: bool = False


class MailProviderError(RuntimeError):
    """A provider failed in a way the caller must handle rather than retry blindly."""


class MailAuthError(MailProviderError):
    """Credentials are expired or revoked.

    Separate from a generic failure because the correct response is to mark the
    account ``REAUTH_REQUIRED`` and ask the human, not to keep retrying - retrying
    an expired token is how an account gets rate-limited or locked.
    """


class MailTransientError(MailProviderError):
    """A timeout, a 5xx, a dropped connection. Safe to retry with backoff."""


@runtime_checkable
class MailTransport(Protocol):
    """What a provider adapter must implement.

    Note what is **not** here: there is no ``send``, no ``reply``, no ``forward``.
    The protocol is the capability ceiling expressed in types.
    """

    name: str

    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> bool:
        """Whether this delivery genuinely came from the provider.

        A webhook endpoint is public. Without signature verification anyone who
        learns the URL can inject mail, which is why this is on the interface
        rather than left to the handler.
        """
        ...

    def list_messages(
        self, *, account: Any, cursor: Optional[str] = None, limit: int = 50
    ) -> SyncBatch:
        """A page of messages, from ``cursor`` or from the start."""
        ...

    def fetch_message(self, *, account: Any, provider_message_id: str) -> InboundMessage:
        """Retrieve one message. Raises rather than returning a partial message."""
        ...

    def fetch_attachment(
        self, *, account: Any, provider_attachment_id: str
    ) -> bytes:
        """Retrieve attachment bytes, after the size policy has approved it."""
        ...


def refuse_send(*, reason: str = "outbound mail is not enabled") -> None:
    """The sanctioned way to attempt an outbound action.

    Raises `ExternalActionDisabled` rather than returning, so no caller can believe
    a send succeeded. Called by the gateway's outbound entry points and by tests
    that need to prove the ceiling holds.
    """
    assert_capability(Capability.MAIL_SEND)
    # Only reachable if the ceiling is ever widened, which Phase 7a must not do.
    raise AssertionError("unreachable while MAIL_SEND is outside the ceiling")
