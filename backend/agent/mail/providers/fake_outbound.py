"""A deterministic outbound provider for tests, including the crash boundaries.

Every scenario the brief names is scriptable by name, so a test that exercises
"provider accepted and the response was lost" is reproducible from the test source
rather than from luck.

**It counts submissions, and that count is the assertion that matters.** The
duplicate-send tests all reduce to: ``provider.submission_count == 1``. A fake that
did not count would let every one of them pass while proving nothing.

Deliberately not random. A fake that fails "sometimes" produces tests that pass
sometimes, and the failures that matter here - a duplicate donor email - are exactly
the ones that must not be intermittent.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agent.mail.outbound import (
    OutboundCapabilityMissing,
    OutboundMessage,
    ReconciliationResult,
    SendFailure,
    SendOutcome,
    SubmitResult,
)

#: The capabilities a send-capable adapter declares. The final authority check reads
#: this, so a fake without it is refused rather than silently used.
SEND_CAPABLE = frozenset({"MAIL_SEND", "MAIL_SEND_HUMAN_APPROVED", "MAIL_RECONCILE_SEND"})
READ_ONLY = frozenset({"MAIL_READ", "MAIL_SYNC"})


@dataclass
class RecordedSubmission:
    """One message the fake provider accepted, kept for assertions."""

    granada_message_ref: str
    idempotency_key: str
    from_address: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    bcc_addresses: tuple[str, ...]
    subject: str
    body_text: str
    attachment_checksums: tuple[str, ...]
    provider_submission_id: str
    provider_message_id: str
    internet_message_id: str
    accepted_at: datetime
    approval_fingerprint: Optional[str]


class FakeOutboundMailProvider:
    """A provider whose behaviour is entirely scripted by the test."""

    def __init__(
        self,
        name: str = "FAKE_OUTBOUND",
        capabilities: frozenset[str] = SEND_CAPABLE,
    ) -> None:
        self.name = name
        self.capabilities = capabilities

        #: Every ACCEPTED submission. The duplicate-send assertions read this.
        self.submissions: list[RecordedSubmission] = []
        #: Every call, accepted or not, so "provider was never called" is checkable.
        self.calls: list[dict[str, Any]] = []
        self.reconciliation_calls: list[str] = []

        # -- scripted behaviour ------------------------------------------
        #: Queue of forced results. Each call pops one; when empty, SUCCESS.
        self.scripted: list[SubmitResult] = []
        #: Fail this many of the next calls with a transient error.
        self.fail_next: int = 0
        self.fail_with: SendFailure = SendFailure.TEMPORARY_FAILURE
        #: Accept internally, then report a network timeout - the case where the
        #: provider HAS the message and Granada does not know.
        self.accept_then_timeout: bool = False
        #: Accept internally, then raise so the caller never writes a result. Used to
        #: simulate a worker crash immediately after provider acceptance.
        self.accept_then_crash: bool = False
        #: Answer reconciliation with this. None means "look in self.submissions",
        #: which is the honest default.
        self.reconciliation_override: Optional[ReconciliationResult] = None
        #: When True, reconciliation claims authoritatively that nothing was found.
        self.reconcile_authoritative_absence: bool = False

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------
    @property
    def submission_count(self) -> int:
        """How many messages the provider actually accepted.

        THE number. Every duplicate-send test asserts this is 1.
        """
        return len(self.submissions)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.submissions.clear()
        self.calls.clear()
        self.reconciliation_calls.clear()
        self.scripted.clear()
        self.fail_next = 0
        self.accept_then_timeout = False
        self.accept_then_crash = False
        self.reconciliation_override = None
        self.reconcile_authoritative_absence = False

    def find_by_ref(self, granada_message_ref: str) -> Optional[RecordedSubmission]:
        for submission in self.submissions:
            if submission.granada_message_ref == granada_message_ref:
                return submission
        return None

    # ------------------------------------------------------------------
    # The OutboundMailProvider interface
    # ------------------------------------------------------------------
    def submit_message(
        self, *, message: OutboundMessage, idempotency_key: str
    ) -> SubmitResult:
        """Hand the message over, honouring the scripted behaviour.

        **Idempotency is enforced here**, as a real provider with an idempotency
        token would. If a submission with the same key already exists, the SAME
        receipt is returned and no second submission is recorded. That is what makes
        the duplicate tests meaningful: they prove Granada's key is stable, not that
        the fake forgot to count.
        """
        with self._lock:
            self.calls.append({
                "granada_message_ref": message.granada_message_ref,
                "idempotency_key": idempotency_key,
                "at": datetime.now(timezone.utc),
            })

            existing = next(
                (s for s in self.submissions if s.idempotency_key == idempotency_key), None
            )
            if existing is not None:
                # A provider-side idempotent replay. Returns the original receipt and
                # does NOT record another submission.
                return SubmitResult(
                    outcome=SendOutcome.CONFIRMED_SENT,
                    provider_submission_id=existing.provider_submission_id,
                    provider_message_id=existing.provider_message_id,
                    internet_message_id=existing.internet_message_id,
                    accepted_at=existing.accepted_at,
                )

            if self.scripted:
                return self.scripted.pop(0)

            if self.fail_next > 0:
                self.fail_next -= 1
                return _failure_result(self.fail_with)

            # -- internal acceptance ------------------------------------
            submission = self._accept(message, idempotency_key)

            if self.accept_then_crash:
                # The provider HAS the message. The process is about to die before it
                # can record that. This is the crash boundary the phase exists for,
                # so it is a BaseException: the send pipeline must NOT catch it.
                self.accept_then_crash = False
                from agent.mail.outbound import WorkerCrash

                raise WorkerCrash(
                    "simulated worker crash after provider acceptance, before "
                    "persistence"
                )

            if self.accept_then_timeout:
                self.accept_then_timeout = False
                # CONFIRMED_SENT internally, DELIVERY_UNKNOWN to the caller. The
                # message exists; Granada does not know it.
                return SubmitResult(
                    outcome=SendOutcome.DELIVERY_UNKNOWN,
                    failure=SendFailure.NETWORK_TIMEOUT,
                    error_code="FAKE_TIMEOUT_AFTER_ACCEPT",
                    safe_error_summary="the response was lost after the provider accepted",
                )

            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_SENT,
                provider_submission_id=submission.provider_submission_id,
                provider_message_id=submission.provider_message_id,
                internet_message_id=submission.internet_message_id,
                accepted_at=submission.accepted_at,
            )

    def _accept(self, message: OutboundMessage, idempotency_key: str) -> RecordedSubmission:
        token = uuid.uuid4().hex
        submission = RecordedSubmission(
            granada_message_ref=message.granada_message_ref or token,
            idempotency_key=idempotency_key,
            from_address=message.from_address,
            to_addresses=tuple(message.to_addresses),
            cc_addresses=tuple(message.cc_addresses),
            bcc_addresses=tuple(message.bcc_addresses),
            subject=message.subject,
            body_text=message.body_text,
            attachment_checksums=tuple(
                str(a.get("checksum_sha256") or "") for a in message.attachments
            ),
            provider_submission_id=f"fake-sub-{token[:16]}",
            provider_message_id=f"fake-msg-{token[:16]}",
            internet_message_id=message.internet_message_id or f"<{token}@granada.example>",
            accepted_at=datetime.now(timezone.utc),
            approval_fingerprint=message.approval_fingerprint,
        )
        self.submissions.append(submission)
        return submission

    def query_submission(
        self, *, granada_message_ref: str, provider_submission_id: Optional[str] = None
    ) -> ReconciliationResult:
        """Establish what actually happened to an earlier attempt."""
        self.reconciliation_calls.append(granada_message_ref)

        if self.reconciliation_override is not None:
            return self.reconciliation_override

        if self.reconcile_authoritative_absence:
            return ReconciliationResult(
                found=False,
                authoritative_absence=True,
                detail="the provider positively reports no such message",
            )

        found = next(
            (s for s in self.submissions if s.granada_message_ref == granada_message_ref),
            None,
        )
        if found is None:
            # Honest: absent from our records, but NOT authoritative, because a real
            # provider that cannot enumerate its sent mail cannot prove a negative.
            return ReconciliationResult(
                found=False,
                authoritative_absence=False,
                detail="no submission with this reference in the provider's records",
            )
        return ReconciliationResult(
            found=True,
            outcome=SendOutcome.CONFIRMED_SENT,
            provider_submission_id=found.provider_submission_id,
            provider_message_id=found.provider_message_id,
            internet_message_id=found.internet_message_id,
            accepted_at=found.accepted_at,
            authoritative_absence=False,
            detail="the provider has this message",
        )


def _failure_result(failure: SendFailure) -> SubmitResult:
    """Map a scripted failure to the correct outcome.

    The mapping is the substance: a definite failure is ``CONFIRMED_NOT_SENT``,
    while an indefinite one must be ``DELIVERY_UNKNOWN``. Getting this backwards is
    how blind retries produce duplicate donor email.
    """
    from agent.mail.outbound import DEFINITE_NOT_SENT

    if failure in DEFINITE_NOT_SENT:
        return SubmitResult(
            outcome=SendOutcome.CONFIRMED_NOT_SENT,
            failure=failure,
            error_code=failure.value,
            safe_error_summary=f"the provider reported {failure.value.lower().replace('_', ' ')}",
            retry_after_seconds=60 if failure == SendFailure.RATE_LIMITED else None,
        )
    return SubmitResult(
        outcome=SendOutcome.DELIVERY_UNKNOWN,
        failure=failure,
        error_code=failure.value,
        safe_error_summary="Granada could not determine whether the provider accepted the message",
    )


class FakeReadOnlyOutboundProvider(FakeOutboundMailProvider):
    """A provider connected with read scope only.

    Exists because the brief requires that read permission must not imply send
    permission. The final authority check reads ``capabilities`` and refuses before
    any call, so a test can prove the refusal rather than trusting the flag.
    """

    def __init__(self) -> None:
        super().__init__(name="FAKE_READ_ONLY", capabilities=READ_ONLY)

    def submit_message(self, *, message: OutboundMessage, idempotency_key: str) -> SubmitResult:
        raise OutboundCapabilityMissing(
            "this account was connected with read-only scope; sending is not available "
            "and no provider call was made"
        )
