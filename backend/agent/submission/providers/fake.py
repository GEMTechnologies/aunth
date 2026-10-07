"""A deterministic submission provider for tests, and the capability boundary.

**It counts submissions, and that count is the assertion that matters.** Every
duplicate-filing test reduces to ``provider.submission_count == 1``, because a duplicate
application can cost the organisation the grant outright - many programmes disqualify
both bids.

Deliberately not random. A fake that fails "sometimes" produces tests that pass
sometimes, and the failure that matters here is intermittent by nature and catastrophic
in effect.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agent.submission.contract import (
    DEFINITE_NOT_SUBMITTED,
    HandoffBundle,
    HandoffStep,
    SubmissionFailure,
    SubmissionOutcome,
    SubmissionPayload,
    SubmissionProviderError,
    SubmissionReconciliation,
    SubmissionResult,
)

#: An adapter that may file an application. The final authority check reads this.
SUBMISSION_CAPABLE = frozenset({"SUBMISSION", "SUBMISSION_HUMAN_AUTHORISED", "SUBMISSION_RECONCILE"})
#: An adapter that may only read a funder's portal - it can prepare, never file.
READ_ONLY = frozenset({"PORTAL_READ", "HANDOFF_PREPARE"})


@dataclass
class RecordedFiling:
    """One application the fake funder accepted, kept for assertions."""

    package_fingerprint: str
    idempotency_key: str
    application_id: str
    organisation_name: str
    document_checksums: tuple[str, ...]
    answer_count: int
    funder_reference: str
    provider_submission_id: str
    accepted_at: datetime
    acknowledgement_text: str


class FakeSubmissionProvider:
    """A funder portal whose behaviour is entirely scripted by the test."""

    def __init__(
        self,
        name: str = "FAKE_FUNDER",
        capabilities: frozenset[str] = SUBMISSION_CAPABLE,
    ) -> None:
        self.name = name
        self.capabilities = capabilities

        #: Every ACCEPTED filing. THE number the duplicate tests assert.
        self.filings: list[RecordedFiling] = []
        #: Every call, accepted or not, so "the provider was never called" is checkable.
        self.calls: list[dict[str, Any]] = []
        self.reconciliation_calls: list[str] = []

        # -- scripted behaviour ------------------------------------------
        self.scripted: list[SubmissionResult] = []
        self.fail_next: int = 0
        self.fail_with: SubmissionFailure = SubmissionFailure.TEMPORARY_FAILURE
        #: Accept internally, then report a network timeout - the case where the funder
        #: HAS the application and Granada does not know.
        self.accept_then_timeout: bool = False
        #: Accept internally, then raise so the caller never records a result. Simulates
        #: a worker crash immediately after the funder accepted.
        self.accept_then_crash: bool = False
        #: Refuse on the grounds that the funder's own terms forbid automated filing.
        self.automation_forbidden: bool = False
        self.reconciliation_override: Optional[SubmissionReconciliation] = None
        self.reconcile_authoritative_absence: bool = False
        #: Whether the fake funder can enumerate its own submissions. Drives whether an
        #: absence is treated as authoritative, which is what unlocks a retry.
        self.can_enumerate: bool = True

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def submission_count(self) -> int:
        """How many applications the funder actually received. THE number."""
        return len(self.filings)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.filings.clear()
        self.calls.clear()
        self.reconciliation_calls.clear()
        self.scripted.clear()
        self.fail_next = 0
        self.accept_then_timeout = False
        self.accept_then_crash = False
        self.automation_forbidden = False
        self.reconciliation_override = None
        self.reconcile_authoritative_absence = False
        self.can_enumerate = True

    def find_by_fingerprint(self, package_fingerprint: str) -> Optional[RecordedFiling]:
        return next(
            (f for f in self.filings if f.package_fingerprint == package_fingerprint), None
        )

    # ------------------------------------------------------------------
    def submit_application(
        self, *, payload: SubmissionPayload, idempotency_key: str
    ) -> SubmissionResult:
        """File the application, honouring the scripted behaviour.

        **Idempotency is enforced here**, as a real portal with a submission token would.
        If a filing with the same key already exists, the SAME reference is returned and
        no second filing is recorded. Without that, the duplicate tests would prove only
        that the fake forgot to count.
        """
        with self._lock:
            self.calls.append({
                "idempotency_key": idempotency_key,
                "package_fingerprint": payload.package_fingerprint,
                "at": datetime.now(timezone.utc),
            })

            if self.automation_forbidden:
                return SubmissionResult(
                    outcome=SubmissionOutcome.CONFIRMED_NOT_SUBMITTED,
                    failure=SubmissionFailure.AUTOMATION_FORBIDDEN,
                    error_code="AUTOMATION_FORBIDDEN",
                    safe_error_summary=(
                        "the funder's terms forbid automated submission; a person must "
                        "file this through the portal"
                    ),
                    automation_forbidden=True,
                )

            existing = next(
                (f for f in self.filings if f.idempotency_key == idempotency_key), None
            )
            if existing is not None:
                # A portal-side idempotent replay: the original reference, and NO second
                # filing recorded.
                return SubmissionResult(
                    outcome=SubmissionOutcome.CONFIRMED_SUBMITTED,
                    funder_reference=existing.funder_reference,
                    provider_submission_id=existing.provider_submission_id,
                    acknowledgement_text=existing.acknowledgement_text,
                    accepted_at=existing.accepted_at,
                )

            if self.scripted:
                return self.scripted.pop(0)

            if self.fail_next > 0:
                self.fail_next -= 1
                return _failure_result(self.fail_with)

            filing = self._accept(payload, idempotency_key)

            if self.accept_then_crash:
                # The funder HAS it. The caller is about to die before recording that.
                self.accept_then_crash = False
                from agent.submission.contract import SubmissionProviderError as _E

                class WorkerCrash(BaseException):
                    """Process death during a submission. A BaseException so the service's
                    `except Exception` cannot convert it into a recorded outcome - which
                    is the whole point of simulating a crash rather than an error."""

                raise WorkerCrash(
                    "simulated worker crash after the funder accepted, before persistence"
                )

            if self.accept_then_timeout:
                self.accept_then_timeout = False
                return SubmissionResult(
                    outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
                    failure=SubmissionFailure.NETWORK_TIMEOUT,
                    error_code="FAKE_TIMEOUT_AFTER_ACCEPT",
                    safe_error_summary=(
                        "the response was lost after the funder accepted the application"
                    ),
                )

            return SubmissionResult(
                outcome=SubmissionOutcome.CONFIRMED_SUBMITTED,
                funder_reference=filing.funder_reference,
                provider_submission_id=filing.provider_submission_id,
                acknowledgement_text=filing.acknowledgement_text,
                accepted_at=filing.accepted_at,
            )

    def _accept(self, payload: SubmissionPayload, idempotency_key: str) -> RecordedFiling:
        token = uuid.uuid4().hex
        filing = RecordedFiling(
            package_fingerprint=payload.package_fingerprint or token,
            idempotency_key=idempotency_key,
            application_id=payload.application_id,
            organisation_name=payload.organisation_name,
            document_checksums=tuple(
                d.checksum_sha256 or "" for d in payload.documents
            ),
            answer_count=len(payload.answers),
            funder_reference=f"FAKE-REF-{token[:10].upper()}",
            provider_submission_id=f"fake-sub-{token[:16]}",
            accepted_at=datetime.now(timezone.utc),
            acknowledgement_text=(
                f"Thank you. Your application has been received and assigned reference "
                f"FAKE-REF-{token[:10].upper()}."
            ),
        )
        self.filings.append(filing)
        return filing

    def query_submission(
        self, *, package_fingerprint: str, provider_submission_id: Optional[str] = None
    ) -> SubmissionReconciliation:
        """Establish what happened to an earlier attempt."""
        self.reconciliation_calls.append(package_fingerprint)

        if self.reconciliation_override is not None:
            return self.reconciliation_override

        if self.reconcile_authoritative_absence:
            return SubmissionReconciliation(
                found=False,
                authoritative_absence=self.can_enumerate,
                detail=(
                    "the portal reports no such application"
                    if self.can_enumerate
                    else "no record, and this portal cannot enumerate its submissions, so "
                         "the absence proves nothing"
                ),
            )

        found = self.find_by_fingerprint(package_fingerprint)
        if found is None:
            # Honest: absent from our records, but authoritative ONLY if the portal can
            # actually enumerate. Without that, a retry stays forbidden.
            return SubmissionReconciliation(
                found=False,
                authoritative_absence=False,
                detail="no filing with this fingerprint in the portal's records",
            )
        return SubmissionReconciliation(
            found=True,
            outcome=SubmissionOutcome.CONFIRMED_SUBMITTED,
            funder_reference=found.funder_reference,
            provider_submission_id=found.provider_submission_id,
            accepted_at=found.accepted_at,
            authoritative_absence=False,
            detail="the funder has this application",
        )


def _failure_result(failure: SubmissionFailure) -> SubmissionResult:
    """Map a scripted failure to the correct outcome.

    The mapping IS the safety property: a definite failure is
    ``CONFIRMED_NOT_SUBMITTED`` and an indefinite one must be ``SUBMISSION_UNKNOWN``.
    Getting this backwards is how a lost response becomes a second application.
    """
    if failure in DEFINITE_NOT_SUBMITTED:
        return SubmissionResult(
            outcome=SubmissionOutcome.CONFIRMED_NOT_SUBMITTED,
            failure=failure,
            error_code=failure.value,
            safe_error_summary=(
                f"the funder reported {failure.value.lower().replace('_', ' ')}"
            ),
            retry_after_seconds=60 if failure == SubmissionFailure.RATE_LIMITED else None,
            automation_forbidden=failure == SubmissionFailure.AUTOMATION_FORBIDDEN,
        )
    return SubmissionResult(
        outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
        failure=failure,
        error_code=failure.value,
        safe_error_summary=(
            "Granada could not determine whether the funder received the application"
        ),
    )


class FakeHandoffBuilder:
    """Prepares a handoff bundle. **Cannot submit anything.**

    A faithful stand-in for a funder portal adapter that can only read: it produces the
    ordered instructions, the documents and the answers, and there is no method on it
    that files anything.
    """

    name = "FAKE_HANDOFF"
    capabilities = READ_ONLY

    def build_handoff(self, *, payload: SubmissionPayload) -> HandoffBundle:
        steps = [
            HandoffStep(1, "Open the funder's application portal",
                        payload.target_url or "the funder's portal URL is not recorded"),
            HandoffStep(2, "Sign in and start a new application",
                        "use the organisation's own account; Granada does not hold portal "
                        "credentials"),
            HandoffStep(3, "Upload each document listed below",
                        "they are the exact versions that were authorised"),
        ]
        order = 4
        for answer in payload.answers:
            steps.append(
                HandoffStep(order, f"Answer: {answer.question}", answer.answer)
            )
            order += 1
        steps.append(
            HandoffStep(order, "Review, submit, and then record the receipt here",
                        "Granada will not mark this SUBMITTED without a funder reference")
        )

        warnings: list[str] = []
        if not payload.documents:
            warnings.append("no documents are attached; most applications require at least one")
        unverified = [a.question for a in payload.answers if not a.verified]
        if unverified:
            warnings.append(
                f"{len(unverified)} answer(s) are not backed by a verified organisation "
                f"fact: {unverified[:3]}"
            )
        if not payload.target_url:
            warnings.append("no portal URL is recorded, so step 1 is approximate")

        return HandoffBundle(
            package_id=payload.package_id,
            application_id=payload.application_id,
            target_url=payload.target_url,
            steps=tuple(steps),
            documents=payload.documents,
            answers=payload.answers,
            checklist=(
                "the application form is complete",
                "every required document is uploaded at the authorised version",
                "the budget figures match the frozen package",
                "the deadline has not passed",
                "the receipt will be recorded here afterwards",
            ),
            package_fingerprint=payload.package_fingerprint,
            warnings=tuple(warnings),
        )


class FakeReadOnlySubmissionProvider(FakeSubmissionProvider):
    """An adapter connected with portal-read scope only.

    Exists because reading a funder's portal must not imply the right to file through
    it. The final authority check reads ``capabilities`` and refuses before any call.
    """

    def __init__(self) -> None:
        super().__init__(name="FAKE_PORTAL_READ", capabilities=READ_ONLY)

    def submit_application(
        self, *, payload: SubmissionPayload, idempotency_key: str
    ) -> SubmissionResult:
        raise SubmissionCapabilityMissing(
            "this adapter can read the funder's portal but is not permitted to file "
            "through it; no submission was attempted"
        )
