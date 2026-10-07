"""The outbound provider contract, and the three outcomes it must distinguish.

Kept separate from the inbound contract on purpose
--------------------------------------------------
Phase 7a's `MailTransport` has no ``send`` method. Phase 7b does **not** add one to
it. Instead there is a second, independent protocol, so that:

    "this mailbox can be read"

can never quietly mean:

    "this mailbox may speak"

A provider may implement inbound only, outbound only, or both. A Gmail account
connected with ``gmail.readonly`` implements inbound and **not** outbound, and the
final authority check refuses a send against it - because the capability genuinely
is not there, not because a flag says so.

The three outcomes
------------------
This is the most important idea in the module.

``CONFIRMED_SENT``
    The provider proved acceptance. A submission id, an accepted message id, an
    unambiguous success response.
``CONFIRMED_NOT_SENT``
    The provider explicitly confirmed it did **not** accept. Safe to consider a
    retry, subject to policy.
``DELIVERY_UNKNOWN``
    Granada does not know. A timeout, a reset connection, a crashed worker, a lost
    response.

**A timeout is not a rejection.** A provider that accepted a message and then had
its response lost is indistinguishable, from Granada's side, from one that never
received it. Treating the first as ``CONFIRMED_NOT_SENT`` and retrying is exactly
how a donor receives the same email twice - and an NGO that double-sends a funder
during a live application has damaged something that took months to build.

So an unknown outcome **forbids** an immediate retry. Reconciliation must first
establish, from provider evidence, that the earlier attempt was not accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class SendOutcome(str, Enum):
    """The three fundamental results. There is no fourth, and no 'probably'."""

    CONFIRMED_SENT = "CONFIRMED_SENT"
    CONFIRMED_NOT_SENT = "CONFIRMED_NOT_SENT"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"


class SendFailure(str, Enum):
    """Why an attempt failed, when it definitely did not succeed.

    Each maps to a *policy* rather than to a message, because the useful question is
    "may we try again, and when" rather than "what did the vendor say".
    """

    #: Definite, and retrying is pointless without a change.
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    #: Definite, and retrying later may work.
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    #: Definite: slow down.
    RATE_LIMITED = "RATE_LIMITED"
    #: Definite: credentials are gone. Retrying an expired token is how an account
    #: gets locked out, so this stops and asks a human.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: Definite: the message itself is unacceptable (bad address, too large).
    INVALID_MESSAGE = "INVALID_MESSAGE"
    #: The provider refused on policy grounds - blocked content, blocked recipient.
    PROVIDER_POLICY_BLOCK = "PROVIDER_POLICY_BLOCK"
    #: Not definite. Granada learned nothing.
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    #: Not definite.
    CONNECTION_RESET = "CONNECTION_RESET"
    #: Not definite. The provider returned something Granada cannot interpret, which
    #: is NOT the same as a failure.
    PROVIDER_ERROR_UNKNOWN = "PROVIDER_ERROR_UNKNOWN"


#: Failures after which the message definitely was not accepted.
DEFINITE_NOT_SENT: frozenset[SendFailure] = frozenset({
    SendFailure.PERMANENT_REJECTION,
    SendFailure.TEMPORARY_FAILURE,
    SendFailure.RATE_LIMITED,
    SendFailure.AUTH_REQUIRED,
    SendFailure.INVALID_MESSAGE,
    SendFailure.PROVIDER_POLICY_BLOCK,
})

#: Failures after which Granada must NOT retry until reconciliation proves the
#: earlier attempt was not accepted.
INDETERMINATE: frozenset[SendFailure] = frozenset({
    SendFailure.NETWORK_TIMEOUT,
    SendFailure.CONNECTION_RESET,
    SendFailure.PROVIDER_ERROR_UNKNOWN,
})

#: Failures that are worth retrying at all, once definitely established.
RETRYABLE: frozenset[SendFailure] = frozenset({
    SendFailure.TEMPORARY_FAILURE,
    SendFailure.RATE_LIMITED,
})


@dataclass
class OutboundMessage:
    """The frozen message handed to a provider.

    A plain value object with no reference back to a draft or an intent. A provider
    adapter must not be able to look up anything: it receives exactly the bytes to
    send, which is what makes "the provider sent what was approved" checkable.
    """

    from_address: str
    to_addresses: tuple[str, ...]
    subject: str
    body_text: str
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    reply_to_address: Optional[str] = None
    #: [{filename, mime_type, content, checksum_sha256}]
    attachments: tuple[dict[str, Any], ...] = ()
    #: Granada's own opaque outbound identity, offered to the provider so a lookup
    #: can find the message even when the provider echoes nothing back.
    granada_message_ref: Optional[str] = None
    #: The Internet Message-ID to use, where the provider permits setting one.
    internet_message_id: Optional[str] = None
    #: The approval fingerprint, so an adapter *can* include it as a header and the
    #: sent message is self-describing about what was authorised.
    approval_fingerprint: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SubmitResult:
    """What the provider said, in Granada's vocabulary."""

    outcome: SendOutcome
    #: Present only for CONFIRMED_SENT.
    provider_submission_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    accepted_at: Optional[datetime] = None
    failure: Optional[SendFailure] = None
    error_code: Optional[str] = None
    #: Safe to persist and show. An adapter is responsible for not echoing message
    #: content into this field.
    safe_error_summary: Optional[str] = None
    #: Set when the provider states when to try again.
    retry_after_seconds: Optional[int] = None
    latency_ms: Optional[int] = None
    raw_provider_reference: Optional[str] = None

    @property
    def is_definite(self) -> bool:
        return self.outcome in (SendOutcome.CONFIRMED_SENT, SendOutcome.CONFIRMED_NOT_SENT)

    @property
    def may_retry_now(self) -> bool:
        """Whether a retry is permissible without reconciliation.

        False for ``DELIVERY_UNKNOWN`` **by construction**, not by a caller
        remembering to check. That is the whole point of the three-way split.
        """
        return (
            self.outcome == SendOutcome.CONFIRMED_NOT_SENT
            and self.failure in RETRYABLE
        )


@dataclass
class ReconciliationResult:
    """What a provider lookup established about an earlier attempt."""

    found: bool
    outcome: Optional[SendOutcome] = None
    provider_submission_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    internet_message_id: Optional[str] = None
    accepted_at: Optional[datetime] = None
    #: True when the provider positively stated it has no record. Only then may a
    #: retry be considered -- "we could not find it" from a provider that also
    #: cannot list its own sent mail is not evidence.
    authoritative_absence: bool = False
    detail: str = ""


class WorkerCrash(BaseException):
    """Simulates the process dying during a provider call.

    Deliberately a ``BaseException`` rather than an ``Exception``, and for a real
    reason rather than a trick: the send pipeline catches ``Exception`` to convert
    provider errors into ``DELIVERY_UNKNOWN``, which is correct for an error and
    wrong for a crash. A genuine crash - SIGKILL, power loss, OOM - is not handled
    by anyone, so nothing is recorded, and the intent is left claiming a send that
    may have happened. That is the state recovery has to cope with, and simulating
    it with a catchable exception would test a different scenario entirely.

    ``KeyboardInterrupt`` and ``SystemExit`` already behave this way; this gives the
    test suite one with an unambiguous name.
    """


class OutboundMailProviderError(RuntimeError):
    """A provider-side error that the caller must handle, not retry blindly."""


class OutboundCapabilityMissing(OutboundMailProviderError):
    """The provider cannot send at all.

    Raised by the final authority check when an account was connected with read-only
    scope. Distinct from a failure because the correct response is to ask for the
    scope, not to retry.
    """


@runtime_checkable
class OutboundMailProvider(Protocol):
    """What an outbound adapter must implement.

    Note the absence of anything inbound: there is no ``fetch_message`` here. An
    adapter for sending cannot accidentally be handed a mailbox to read.

    ``capabilities`` is explicit rather than implied, because the brief requires
    that a read scope must not imply a send scope. The final authority check reads
    it and refuses when ``MAIL_SEND`` is absent.
    """

    name: str
    capabilities: frozenset[str]

    def submit_message(self, *, message: OutboundMessage, idempotency_key: str) -> SubmitResult:
        """Hand the message to the provider.

        ``idempotency_key`` is passed so an adapter that supports a provider-side
        idempotency token can use it, and a duplicate submission is recognisable
        rather than merely hoped against. An adapter that does not support one must
        say so in its ``capabilities`` rather than silently ignoring the argument.
        """
        ...

    def query_submission(
        self, *, granada_message_ref: str, provider_submission_id: Optional[str] = None
    ) -> ReconciliationResult:
        """Establish what happened to an earlier attempt.

        This is what turns ``DELIVERY_UNKNOWN`` into a definite answer. An adapter
        that cannot look messages up must return ``found=False`` with
        ``authoritative_absence=False`` - the honest answer, and one that forbids a
        retry rather than permitting a duplicate.
        """
        ...
