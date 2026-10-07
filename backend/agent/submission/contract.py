"""The submission contract: what an adapter receives, and the three outcomes.

Deliberately the same shape as ``agent.mail.outbound``
------------------------------------------------------
Phase 7b established a pattern for irreversible external actions and this phase reuses
it rather than inventing a second one:

* a **frozen** package whose fingerprint is what a human authorises;
* a provider protocol with explicit ``capabilities``;
* **three** outcomes, with uncertainty never collapsed into failure;
* append-only attempts, because an attempt whose result was never learned is the only
  evidence reconciliation has.

Two protocols separated for the same reason as before
-----------------------------------------------------
``SubmissionProvider`` can *submit*. ``HandoffBuilder`` cannot - it prepares a bundle
for a person to submit in the funder's own portal. Keeping them apart means "this
adapter can prepare a checklist" can never quietly become "this adapter can file an
application", which is the same discipline that stopped a mailbox that could be read
from becoming one that could speak.

Why the three outcomes matter more here than for email
------------------------------------------------------
An email can be apologised for. A duplicate application cannot: many programmes
disqualify **both** bids from an organisation that appears to have submitted twice, so
an uncertain submission that gets retried can cost the grant outright and damage the
relationship with the funder.

So a timeout during submission is ``SUBMISSION_UNKNOWN``, a retry is forbidden until
reconciliation produces evidence, and ``may_retry_now`` is False for anything uncertain
*by construction* rather than because a caller remembered to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class SubmissionOutcome(str, Enum):
    """The three fundamental results. There is no fourth, and no 'probably'."""

    CONFIRMED_SUBMITTED = "CONFIRMED_SUBMITTED"
    CONFIRMED_NOT_SUBMITTED = "CONFIRMED_NOT_SUBMITTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


class SubmissionFailure(str, Enum):
    """Why an attempt definitely did not succeed, mapped to a *policy* not a message."""

    #: The funder's system rejected it: wrong form, missing field, closed programme.
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    #: Definite, and retrying later may work - a portal outage, a maintenance window.
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    #: Definite: slow down, or risk being blocked.
    RATE_LIMITED = "RATE_LIMITED"
    #: Definite: the session or credentials are gone. Retrying is pointless and may
    #: lock the account.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: The package itself is unacceptable - an oversize upload, an unsupported format.
    INVALID_PACKAGE = "INVALID_PACKAGE"
    #: The deadline passed. Distinct from a rejection because the remedy is different:
    #: nothing can be done about this one.
    DEADLINE_PASSED = "DEADLINE_PASSED"
    #: The service's terms forbid automated submission. A refusal on principle, and it
    #: must never be retried by a different route.
    AUTOMATION_FORBIDDEN = "AUTOMATION_FORBIDDEN"
    #: Not definite.
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    #: Not definite.
    CONNECTION_RESET = "CONNECTION_RESET"
    #: Not definite. The provider answered something Granada cannot interpret, which is
    #: NOT the same as a rejection.
    PROVIDER_ERROR_UNKNOWN = "PROVIDER_ERROR_UNKNOWN"


#: Failures after which the application definitely was not filed.
DEFINITE_NOT_SUBMITTED: frozenset[SubmissionFailure] = frozenset({
    SubmissionFailure.PERMANENT_REJECTION,
    SubmissionFailure.TEMPORARY_FAILURE,
    SubmissionFailure.RATE_LIMITED,
    SubmissionFailure.AUTH_REQUIRED,
    SubmissionFailure.INVALID_PACKAGE,
    SubmissionFailure.DEADLINE_PASSED,
    SubmissionFailure.AUTOMATION_FORBIDDEN,
})

#: Failures after which Granada must NOT retry until reconciliation proves the earlier
#: attempt did not land.
INDETERMINATE: frozenset[SubmissionFailure] = frozenset({
    SubmissionFailure.NETWORK_TIMEOUT,
    SubmissionFailure.CONNECTION_RESET,
    SubmissionFailure.PROVIDER_ERROR_UNKNOWN,
})

#: Worth retrying at all, once definitely established.
RETRYABLE: frozenset[SubmissionFailure] = frozenset({
    SubmissionFailure.TEMPORARY_FAILURE,
    SubmissionFailure.RATE_LIMITED,
})


@dataclass
class FrozenDocument:
    """One document as it will be filed: a specific version, with its checksum."""

    document_id: str
    doc_type: Optional[str] = None
    version: Optional[int] = None
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    checksum_sha256: Optional[str] = None
    storage_ref: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "version": self.version,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "checksum_sha256": self.checksum_sha256,
            "storage_ref": self.storage_ref,
        }


@dataclass
class FrozenAnswer:
    """One answer, with where it came from.

    The provenance is not decoration. A funder asking "do you have a safeguarding
    policy", answered from a VERIFIED organisation fact, is a different statement from
    the same string generated by a model - and the organisation is accountable for the
    answer either way.
    """

    question: str
    answer: str
    source: str
    #: Named so an unverified answer is visible rather than merely present.
    verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "source": self.source,
            "verified": self.verified,
        }


@dataclass
class SubmissionPayload:
    """The frozen package handed to a provider or a person.

    A plain value object with no reference back to the application. An adapter must not
    be able to look anything up: it receives exactly what was authorised, which is what
    makes "the funder received what the organisation approved" checkable.
    """

    package_id: str
    application_id: str
    organisation_name: str
    opportunity_title: str
    target_url: Optional[str] = None
    documents: tuple[FrozenDocument, ...] = ()
    answers: tuple[FrozenAnswer, ...] = ()
    budget: Optional[dict[str, Any]] = None
    contact_email: Optional[str] = None
    package_fingerprint: Optional[str] = None
    #: Attached to the payload where the provider allows it, so a filed application is
    #: self-describing about what was authorised.
    reference_header: Optional[str] = None


@dataclass
class SubmissionResult:
    """What the provider said, in Granada's vocabulary."""

    outcome: SubmissionOutcome
    #: Present only for CONFIRMED_SUBMITTED.
    funder_reference: Optional[str] = None
    provider_submission_id: Optional[str] = None
    acknowledgement_text: Optional[str] = None
    accepted_at: Optional[datetime] = None
    failure: Optional[SubmissionFailure] = None
    error_code: Optional[str] = None
    safe_error_summary: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    latency_ms: Optional[int] = None
    #: True when the funder's own terms forbid automated submission. Recorded so an
    #: operator can see that the refusal is a policy, not an outage.
    automation_forbidden: bool = False

    @property
    def may_retry_now(self) -> bool:
        """Whether a retry is permissible without reconciliation.

        False for ``SUBMISSION_UNKNOWN`` **by construction**. A duplicate application
        can cost the grant outright, so the answer cannot depend on a caller checking.
        """
        return (
            self.outcome == SubmissionOutcome.CONFIRMED_NOT_SUBMITTED
            and self.failure in RETRYABLE
        )


@dataclass
class SubmissionReconciliation:
    """What a lookup established about an earlier attempt."""

    found: bool
    outcome: Optional[SubmissionOutcome] = None
    funder_reference: Optional[str] = None
    provider_submission_id: Optional[str] = None
    accepted_at: Optional[datetime] = None
    #: True only when the provider positively states it has no record *and* is able to
    #: enumerate its own submissions. Without that, an absence proves nothing.
    authoritative_absence: bool = False
    detail: str = ""


@dataclass
class HandoffStep:
    """One instruction in a handoff bundle."""

    order: int
    instruction: str
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"order": self.order, "instruction": self.instruction, "detail": self.detail}


@dataclass
class HandoffBundle:
    """Everything a person needs to file an application themselves.

    **This is the Phase 8 mode that is implemented fully, and it performs no external
    action at all.** Most funders use their own portals; a handoff gives the
    organisation the exact authorised artefacts, in order, with the answers ready to
    paste - and the human remains the one who submits, so nothing irreversible happens
    without a person doing it deliberately.
    """

    package_id: str
    application_id: str
    target_url: Optional[str]
    steps: tuple[HandoffStep, ...] = ()
    documents: tuple[FrozenDocument, ...] = ()
    answers: tuple[FrozenAnswer, ...] = ()
    checklist: tuple[str, ...] = ()
    package_fingerprint: Optional[str] = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "application_id": self.application_id,
            "target_url": self.target_url,
            "steps": [s.as_dict() for s in self.steps],
            "documents": [d.as_dict() for d in self.documents],
            "answers": [a.as_dict() for a in self.answers],
            "checklist": list(self.checklist),
            "package_fingerprint": self.package_fingerprint,
            "warnings": list(self.warnings),
        }


class SubmissionProviderError(RuntimeError):
    """A provider-side error the caller must handle rather than retry blindly."""


class SubmissionCapabilityMissing(SubmissionProviderError):
    """The provider cannot submit at all.

    Raised by the final authority check when an adapter's ``capabilities`` omit
    ``SUBMISSION``. Distinct from a failure because the correct response is to
    configure an adapter, not to retry.
    """


@runtime_checkable
class HandoffBuilder(Protocol):
    """Prepares a bundle for a person. Cannot submit anything.

    Note the absence of ``submit``. That is the guarantee, not a convention.
    """

    name: str

    def build_handoff(self, *, payload: SubmissionPayload) -> HandoffBundle:
        ...


@runtime_checkable
class SubmissionProvider(Protocol):
    """Files an application. **No implementation of this is enabled.**

    ``capabilities`` is explicit rather than implied, so an adapter that can read a
    funder's portal cannot be mistaken for one that may file through it. The final
    authority check reads it and refuses when ``SUBMISSION`` is absent.
    """

    name: str
    capabilities: frozenset[str]

    def submit_application(
        self, *, payload: SubmissionPayload, idempotency_key: str
    ) -> SubmissionResult:
        ...

    def query_submission(
        self, *, package_fingerprint: str, provider_submission_id: Optional[str] = None
    ) -> SubmissionReconciliation:
        ...
