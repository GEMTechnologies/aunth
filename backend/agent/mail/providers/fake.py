"""A deterministic mail provider for tests, and the contract for real ones.

Everything the brief lists is simulatable here by name - duplicate webhooks,
out-of-order delivery, timeouts, retrieval failures, expired credentials - so the
gateway's behaviour under each is a test rather than a hope.

**It is deliberately not random.** A fake that fails "sometimes" produces tests
that pass sometimes. Failure is requested explicitly through ``fail_next`` or the
scripted behaviours, so a failing run is reproducible from the test source alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agent.mail.providers.base import (
    InboundAttachment,
    InboundMessage,
    MailAuthError,
    MailTransientError,
    ProviderEvent,
    SyncBatch,
)


@dataclass
class FakeAccount:
    """Stands in for a `mail_accounts` row without requiring one."""

    provider_account_id: str
    address: str
    credentials_valid: bool = True
    token: Optional[str] = "fake-token"


@dataclass
class FakeMailbox:
    """The scripted state of one fake mailbox."""

    account: FakeAccount
    messages: dict[str, InboundMessage] = field(default_factory=dict)
    threads: dict[str, list[str]] = field(default_factory=dict)
    attachment_bytes: dict[str, bytes] = field(default_factory=dict)
    #: The order ``list_messages`` will return. Lets a test deliver a reply before
    #: the message it replies to, which is the out-of-order case.
    order: list[str] = field(default_factory=list)
    cursor: int = 0


class FakeMailProvider:
    """A provider whose behaviour is entirely scripted by the test."""

    name = "FAKE"

    #: Webhook signatures this provider will accept. Empty means accept all, which
    #: is the test-suite default; ``reject_webhooks`` flips it.
    valid_signatures: set[str] = set()
    reject_webhooks = False

    def __init__(self) -> None:
        self.mailboxes: dict[str, FakeMailbox] = {}
        self.webhook_calls = 0
        self.fetch_calls: list[str] = []
        self.attachment_calls: list[str] = []
        self.sync_calls: list[tuple[Optional[str], int]] = []

        # -- scripted failures, set by a test ----------------------------
        #: How many of the next calls of each kind should fail.
        self.fail_fetch_times = 0
        self.fail_attachment_times = 0
        self.fail_sync_times = 0
        #: When True, ``fetch_message`` returns a message missing its body, which
        #: is the "provider gave us a stub" case rather than an outright failure.
        self.return_partial_message = False
        self.expire_credentials_after = 0

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------
    def add_account(
        self, *, provider_account_id: str, address: str, credentials_valid: bool = True
    ) -> FakeAccount:
        account = FakeAccount(
            provider_account_id=provider_account_id,
            address=address,
            credentials_valid=credentials_valid,
        )
        self.mailboxes[provider_account_id] = FakeMailbox(account=account)
        return account

    def add_message(
        self,
        account: FakeAccount,
        *,
        provider_message_id: str,
        subject: Optional[str] = None,
        sender: Optional[str] = None,
        sender_name: Optional[str] = None,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        internet_message_id: Optional[str] = None,
        references: tuple[str, ...] = (),
        provider_thread_id: Optional[str] = None,
        recipients: tuple[str, ...] = (),
        received_at: Optional[datetime] = None,
        authentication_results: Optional[dict[str, Any]] = None,
        attachments: tuple[InboundAttachment, ...] = (),
        headers: Optional[dict[str, str]] = None,
    ) -> InboundMessage:
        message = InboundMessage(
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id or f"thread-{provider_message_id}",
            internet_message_id=internet_message_id or f"<{provider_message_id}@example.org>",
            in_reply_to=in_reply_to,
            references=references,
            sender=sender,
            sender_name=sender_name,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            received_at=received_at or datetime.now(timezone.utc),
            authentication_results=authentication_results or {},
            attachments=attachments,
            headers=headers or {},
        )
        mailbox = self.mailboxes[account.provider_account_id]
        mailbox.messages[provider_message_id] = message
        mailbox.threads.setdefault(message.provider_thread_id or "none", []).append(
            provider_message_id
        )
        if provider_message_id not in mailbox.order:
            mailbox.order.append(provider_message_id)
        return message

    def add_attachment_bytes(self, provider_attachment_id: str, content: bytes) -> None:
        self.attachment_bytes[provider_attachment_id] = content

    def webhook(
        self,
        *,
        provider_event_id: str,
        event_type: str = "message.received",
        provider_account_id: str,
        provider_message_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> ProviderEvent:
        return ProviderEvent(
            provider_event_id=provider_event_id,
            event_type=event_type,
            provider_account_id=provider_account_id,
            provider_message_id=provider_message_id,
            payload=payload or {},
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # The MailTransport interface
    # ------------------------------------------------------------------
    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> bool:
        self.webhook_calls += 1
        if self.reject_webhooks:
            return False
        signature = headers.get("x-fake-signature")
        if self.valid_signatures and signature not in self.valid_signatures:
            return False
        return True

    def _check_credentials(self, account: FakeAccount) -> None:
        if not account.credentials_valid:
            raise MailAuthError("credentials are expired or revoked")

    def list_messages(
        self, *, account: Any, cursor: Optional[str] = None, limit: int = 50
    ) -> SyncBatch:
        self.sync_calls.append((cursor, limit))
        mailbox = self.mailboxes[account.provider_account_id]
        self._check_credentials(mailbox.account)

        if self.fail_sync_times > 0:
            self.fail_sync_times -= 1
            raise MailTransientError("provider timed out")

        start = int(cursor) if cursor else 0
        ids = mailbox.order[start : start + limit]
        messages = tuple(mailbox.messages[i] for i in ids)
        next_index = start + len(ids)
        has_more = next_index < len(mailbox.order)
        return SyncBatch(
            messages=messages,
            next_cursor=str(next_index) if has_more else None,
            has_more=has_more,
        )

    def fetch_message(self, *, account: Any, provider_message_id: str) -> InboundMessage:
        self.fetch_calls.append(provider_message_id)
        mailbox = self.mailboxes.get(account.provider_account_id)
        if mailbox is None:
            raise MailTransientError(f"unknown account {account.provider_account_id}")
        self._check_credentials(mailbox.account)

        if self.fail_fetch_times > 0:
            self.fail_fetch_times -= 1
            raise MailTransientError("provider timed out")

        message = mailbox.messages.get(provider_message_id)
        if message is None:
            raise MailTransientError(f"message {provider_message_id} not found")

        if self.return_partial_message:
            # A provider that returns a stub must not be treated as a message with
            # no body: the difference between "empty" and "we could not read it"
            # is the difference between a wrong classification and a retry.
            return InboundMessage(
                provider_message_id=message.provider_message_id,
                provider_thread_id=message.provider_thread_id,
                sender=message.sender,
                received_at=message.received_at,
            )
        return message

    def fetch_attachment(self, *, account: Any, provider_attachment_id: str) -> bytes:
        self.attachment_calls.append(provider_attachment_id)
        if self.fail_attachment_times > 0:
            self.fail_attachment_times -= 1
            raise MailTransientError("attachment retrieval failed")
        if provider_attachment_id not in self.attachment_bytes:
            raise MailTransientError(f"no content for {provider_attachment_id}")
        return self.attachment_bytes[provider_attachment_id]
