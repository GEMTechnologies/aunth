"""Provider-neutral mail adapters. See `base` for the capability boundary."""

from agent.mail.providers.base import (
    InboundAttachment,
    InboundMessage,
    MailAuthError,
    MailProviderError,
    MailTransientError,
    MailTransport,
    ProviderEvent,
    SyncBatch,
    refuse_send,
)
from agent.mail.providers.fake import FakeAccount, FakeMailProvider
from agent.mail.vocabulary import ExternalActionDisabled

__all__ = [
    "ExternalActionDisabled",
    "FakeAccount",
    "FakeMailProvider",
    "InboundAttachment",
    "InboundMessage",
    "MailAuthError",
    "MailProviderError",
    "MailTransientError",
    "MailTransport",
    "ProviderEvent",
    "SyncBatch",
    "refuse_send",
]