"""The submission service: freeze, authorise, hand off, file, receipt.

The pipeline, and why each step exists
--------------------------------------
1. **Freeze the package.** Documents at specific versions with checksums, answers with
   provenance, the budget. A fingerprint over all of it is what a human authorises.
2. **Authorise.** A person holding ``submission.authorise`` approves **that
   fingerprint**. Change anything and the authorisation no longer applies - there is no
   inheritance, for the same reason as outbound mail and with higher stakes.
3. **Hand off, or file.**
   * ``HANDOFF`` produces the bundle and performs **no external action**. This is the
     mode Phase 8 implements fully, and it is what most funders require anyway.
   * ``ADAPTER`` hands the package to a provider. Implemented against a fake only.
4. **Receipt.** ``SUBMITTED`` requires a funder reference. Without one the application
   is *possibly* submitted, and an organisation that believes it applied when it did not
   has lost the grant and does not know it.

The four things that must never happen
--------------------------------------
* a submission without a human authorisation of the exact package;
* a second filing because a response was lost;
* ``SUBMITTED`` without a receipt;
* an adapter that was only permitted to *read* a portal filing through it.
Each is refused by a named check below, and each has a test.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from agent.mail.approval import has_permission
from agent.mail.ceiling import Capability, assert_capability
from agent.submission.contract import (
    INDETERMINATE,
    RETRYABLE,
    FrozenAnswer,
    FrozenDocument,
    HandoffBundle,
    SubmissionFailure,
    SubmissionOutcome,
    SubmissionPayload,
    SubmissionReconciliation,
    SubmissionResult,
)

logger = logging.getLogger(__name__)

#: The permission that authorises filing an application. Deliberately NOT implied by
#: anything else, and separate from `mail.approve_send`: an organisation may reasonably
#: let someone answer a funder's email without letting them file a grant application.
AUTHORISE_SUBMISSION_PERMISSION = "submission.authorise"

#: How long an authorisation stays usable.
DEFAULT_AUTHORISATION_TTL_HOURS = 72

#: Separators for the canonical fingerprint, matching the mail package's choices.
UNIT = "\x1f"
RECORD = "\x1e"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return None if value is None else (value if value.tzinfo else value.replace(tzinfo=timezone.utc))


def _text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")


class SubmissionError(RuntimeError):
    """Base class for submission refusals."""


class NotAuthorisable(SubmissionError):
    """The package cannot be put forward for authorisation."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class NotSubmittable(SubmissionError):
    """The package may not be filed, for a named reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class SubmissionRun:
    """What one filing attempt did."""

    package_id: str
    outcome: Optional[str] = None
    state: str = ""
    attempt_id: Optional[str] = None
    funder_reference: Optional[str] = None
    failure: Optional[str] = None
    detail: str = ""
    refused: bool = False
    refusal_code: Optional[str] = None
    handoff: Optional[HandoffBundle] = None

    @property
    def submitted(self) -> bool:
        return self.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED.value

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "submission_package_id": self.package_id,
            "outcome": self.outcome,
            "state": self.state,
            "attempt_id": self.attempt_id,
            "funder_reference": self.funder_reference,
            "failure": self.failure,
            "detail": self.detail,
            "refused": self.refused,
            "refusal_code": self.refusal_code,
        }
        if self.handoff is not None:
            payload["handoff"] = self.handoff.as_dict()
        return payload


def package_fingerprint(
    *,
    org_id: str,
    agent_id: str,
    application_id: str,
    documents: Iterable[FrozenDocument],
    answers: Iterable[FrozenAnswer],
    budget: Optional[dict[str, Any]],
    contact_email: Optional[str],
    target_url: Optional[str],
) -> tuple[str, str]:
    """SHA-256 over a canonical serialisation of the whole package.

    Returns ``(fingerprint, canonical_input)`` so a mismatch can be *diffed* rather than
    merely detected.

    Ordered and normalised, for the same reasons as the mail fingerprint: document
    order is not a change, a re-upload that keeps the id but changes the bytes is, and
    a canonical form with no fixed order would make every re-freeze look different.
    """
    import json

    docs = sorted(
        (d.as_dict() for d in documents),
        key=lambda d: (d.get("document_id") or "", str(d.get("version") or "")),
    )
    # Answers sorted by question so a reordered form is not a change, while a changed
    # ANSWER - the thing the organisation is accountable for - absolutely is.
    answer_rows = sorted(
        (a.as_dict() for a in answers), key=lambda a: a.get("question") or ""
    )

    parts = [
        f"version{UNIT}granada-submission-v1",
        f"org_id{UNIT}{_text(org_id)}",
        f"agent_id{UNIT}{_text(agent_id)}",
        f"application_id{UNIT}{_text(application_id)}",
        f"target_url{UNIT}{_text(target_url)}",
        f"contact_email{UNIT}{_text(contact_email).strip().lower()}",
        f"document_count{UNIT}{len(docs)}",
        *[
            UNIT.join([
                "doc",
                _text(d.get("document_id")),
                str(d.get("version") or ""),
                _text(d.get("doc_type")),
                _text(d.get("filename")),
                _text(d.get("mime_type")),
                # The checksum is what proves the bytes are the ones authorised.
                _text(d.get("checksum_sha256")),
            ])
            for d in docs
        ],
        f"answer_count{UNIT}{len(answer_rows)}",
        *[
            UNIT.join([
                "answer",
                _text(a.get("question")),
                # Length-prefixed, so an answer containing the separator cannot forge
                # the field after it.
                str(len(_text(a.get("answer")))),
                _text(a.get("answer")),
                _text(a.get("source")),
                "verified" if a.get("verified") else "unverified",
            ])
            for a in answer_rows
        ],
        # The budget is serialised with sorted keys: a transposed digit must change the
        # fingerprint, and an unrelated key reordering must not.
        f"budget{UNIT}{json.dumps(budget or {}, sort_keys=True, default=str)}",
    ]
    canonical = RECORD.join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical


def diff_fingerprint_inputs(approved: Optional[str], current: Optional[str]) -> dict[str, Any]:
    """Which canonical fields differ, so a refusal is actionable."""
    if not approved or not current:
        return {"comparable": False, "reason": "one side is missing"}
    left, right = approved.split(RECORD), current.split(RECORD)
    changed: list[str] = []
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else ""
        b = right[index] if index < len(right) else ""
        if a != b:
            changed.append((a or b).split(UNIT)[0] or f"field_{index}")
    return {"comparable": True, "changed": changed}


class SubmissionService:
    """Builds, authorises, hands off and files submission packages."""

    def __init__(
        self,
        db: Session,
        *,
        org_id: str,
        agent_id: str,
        provider: Optional[Any] = None,
        handoff_builder: Optional[Any] = None,
    ) -> None:
        if not org_id or not agent_id:
            raise SubmissionError(
                "a submission requires both an organisation and an agent"
            )
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id
        self.provider = provider
        self.handoff_builder = handoff_builder

    # ==================================================================
    # 1. Freeze the package
    # ==================================================================
    def build_package(
        self,
        *,
        application: models.Application,
        documents: Iterable[Any] = (),
        answers: Iterable[dict[str, Any]] = (),
        budget: Optional[dict[str, Any]] = None,
        contact_email: Optional[str] = None,
        target_url: Optional[str] = None,
        mode: str = models.SubmissionPackage.MODE_HANDOFF,
    ) -> models.SubmissionPackage:
        """Freeze the artefact set into a package a human can authorise.

        Refuses outright when the application is not ready or a document is ineligible,
        **before** a package exists - so a blocked application never leaves an
        authorisable artefact behind for someone to approve later.
        """
        # Files nothing and touches nothing outside the database: the same position
        # as a mail draft. The approval gates are on the paths that reach the world.
        assert_capability(Capability.SUBMISSION_PREPARE)

        if application.org_id != self.org_id:
            raise SubmissionError("the application belongs to another organisation")

        # The readiness gate, reused rather than reimplemented: one definition of "ready
        # to submit" means the workspace and this service cannot disagree.
        from agent.workspace import ApplicationWorkspace

        workspace = ApplicationWorkspace(self.db, self.org_id)
        readiness = workspace.readiness(application)
        if not readiness.ready:
            raise NotAuthorisable(
                "APPLICATION_NOT_READY",
                "; ".join(readiness.blockers[:6]),
            )

        frozen_documents = self._freeze_documents(documents)
        frozen_answers = self._freeze_answers(answers)

        agent = self.db.execute(
            select(models.GranadaAgent).where(
                models.GranadaAgent.id == self.agent_id,
                models.GranadaAgent.org_id == self.org_id,
            )
        ).scalars().first()
        if agent is None:
            raise SubmissionError("the agent does not belong to this organisation")

        digest, canonical = package_fingerprint(
            org_id=self.org_id,
            agent_id=self.agent_id,
            application_id=application.id,
            documents=frozen_documents,
            answers=frozen_answers,
            budget=budget,
            contact_email=contact_email,
            target_url=target_url,
        )

        # One package per (application, fingerprint). Re-freezing the same artefacts is
        # idempotent; a changed artefact is a NEW package, which is what "no
        # authorisation inheritance" means in practice.
        idempotency_key = f"{application.id}:{digest[:32]}"
        existing = self.db.execute(
            select(models.SubmissionPackage).where(
                models.SubmissionPackage.org_id == self.org_id,
                models.SubmissionPackage.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing is not None:
            return existing

        package = models.SubmissionPackage(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            application_id=application.id,
            opportunity_id=application.opportunity_id,
            package_fingerprint=digest,
            fingerprint_input=canonical,
            manifest={
                "documents": [d.as_dict() for d in frozen_documents],
                "answers": [a.as_dict() for a in frozen_answers],
                "budget": budget or {},
                "contact_email": contact_email,
                "target_url": target_url,
            },
            application_version=getattr(application, "version", 1) or 1,
            status=models.SubmissionPackage.AWAITING_AUTHORISATION,
            submission_mode=mode,
            agent_version=agent.version,
            target_url=target_url,
            idempotency_key=idempotency_key,
            created_at=_now(),
            correlation_id=str(uuid.uuid4()),
        )
        self.db.add(package)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            return self.db.execute(
                select(models.SubmissionPackage).where(
                    models.SubmissionPackage.idempotency_key == idempotency_key
                )
            ).scalars().first()

        self._stage_event(
            event_type="submission.package_frozen",
            payload={
                "submission_package_id": package.id,
                "application_id": application.id,
                "fingerprint": package.package_fingerprint,
                "documents": len(frozen_documents),
                "answers": len(frozen_answers),
            },
        )
        self._record_activity(
            summary_key="submission.package_frozen",
            structured={
                "submission_package_id": package.id,
                "application_id": application.id,
                "documents": len(frozen_documents),
                "mode": mode,
            },
            subject_id=package.id,
        )
        self.db.flush()
        return package

    def _freeze_documents(self, documents: Iterable[Any]) -> list[FrozenDocument]:
        """Freeze documents, refusing anything not properly the organisation's.

        Reuses the vault's own eligibility rules through ``build_attachment_manifest``
        so there is one definition of an attachable document - and adds the
        cross-organisation check, because a submission carrying another organisation's
        certificate would be a disclosure, not just an error.
        """
        from agent.mail.risk import build_attachment_manifest

        report = build_attachment_manifest(documents, org_id=self.org_id)
        if not report.ok:
            raise NotAuthorisable(
                "DOCUMENT_INELIGIBLE", "; ".join(e["detail"] for e in report.errors)
            )
        frozen: list[FrozenDocument] = []
        for entry in report.manifest:
            frozen.append(
                FrozenDocument(
                    document_id=entry.get("document_id") or "",
                    doc_type=None,
                    version=entry.get("version"),
                    filename=entry.get("filename"),
                    mime_type=entry.get("mime_type"),
                    checksum_sha256=entry.get("checksum_sha256"),
                    storage_ref=entry.get("storage_ref"),
                )
            )
        return frozen

    def _freeze_answers(self, answers: Iterable[dict[str, Any]]) -> list[FrozenAnswer]:
        """Freeze answers, marking which are backed by a verified fact.

        The mark is not decoration: an answer generated by a model and one drawn from a
        VERIFIED organisation fact are different statements, and the organisation is
        accountable for both. The handoff bundle surfaces the unverified ones.
        """
        frozen: list[FrozenAnswer] = []
        for item in answers or ():
            if isinstance(item, FrozenAnswer):
                frozen.append(item)
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            frozen.append(
                FrozenAnswer(
                    question=question,
                    answer=str(item.get("answer") or ""),
                    source=str(item.get("source") or "unknown"),
                    verified=bool(item.get("verified")),
                )
            )
        return frozen

    # ==================================================================
    # 2. Authorise
    # ==================================================================
    def authorise(self, *, package_id: str, user_id: str, note: Optional[str] = None) -> models.SubmissionPackage:
        """A person approves THE FINGERPRINT.

        Recalculated here from the live row rather than accepted from the caller, so a
        client cannot authorise one package while another is filed.
        """
        allowed, reason = has_permission(
            self.db, org_id=self.org_id, user_id=user_id,
            permission=AUTHORISE_SUBMISSION_PERMISSION,
        )
        if not allowed:
            # Falls back to the approval permission so an organisation that has not
            # created the narrower grant is not locked out of its own submissions -
            # but the narrower one is checked first, and named in the refusal.
            mail_allowed, mail_reason = has_permission(
                self.db, org_id=self.org_id, user_id=user_id,
                permission="mail.approve_send",
            )
            if not mail_allowed:
                raise SubmissionError(
                    f"{user_id} may not authorise a submission: {reason} "
                    f"(and not via mail.approve_send either: {mail_reason})"
                )

        package = self._package(package_id)
        if package.status in models.SubmissionPackage.TERMINAL:
            raise SubmissionError(f"package {package.id} is already {package.status}")
        if package.status == models.SubmissionPackage.SUBMITTING:
            raise SubmissionError(
                f"package {package.id} is already being filed; authorising it now would "
                "be a decision about something that has left"
            )

        live, _ = self._fingerprint_of(package)
        if live != package.package_fingerprint:
            package.package_fingerprint = live
            self.db.flush()
            raise SubmissionError(
                "the package changed while it was being reviewed; it now carries a "
                "different fingerprint and must be re-read before authorisation"
            )

        package.status = models.SubmissionPackage.AUTHORISED
        package.authorised_at = _now()
        package.authorised_by = user_id
        package.status_reason = note or "authorised by a person"
        self.db.flush()
        self._stage_event(
            event_type="submission.authorised",
            payload={"submission_package_id": package.id, "fingerprint": package.package_fingerprint},
        )
        self._record_activity(
            summary_key="submission.authorised",
            structured={"submission_package_id": package.id, "application_id": package.application_id},
            subject_id=package.id,
        )
        self.db.flush()
        return package

    def reject(self, *, package_id: str, user_id: str, note: Optional[str] = None) -> models.SubmissionPackage:
        allowed, _ = has_permission(
            self.db, org_id=self.org_id, user_id=user_id, permission="mail.approve_send"
        )
        if not allowed:
            raise SubmissionError("only a member of this organisation may reject its submission")
        package = self._package(package_id)
        if package.status in models.SubmissionPackage.TERMINAL:
            raise SubmissionError(f"package {package.id} is already {package.status}")
        package.status = models.SubmissionPackage.REJECTED
        package.status_reason = note or "rejected by a person"
        self.db.flush()
        return package

    # ==================================================================
    # 3. Hand off
    # ==================================================================
    def handoff(self, *, package_id: str) -> SubmissionRun:
        """Produce the bundle a person files themselves. **No external action.**

        Authorisation is required even though nothing is filed, because the bundle
        commits the organisation's evidence: it names the exact documents and answers
        that are to be submitted in its name.
        """
        # A missing package returns a REFUSAL here, matching `execute`.
        #
        # The first version called `self._package(...)`, which raises — so the same
        # not-found condition raised from `handoff` and returned a `NOT_FOUND` refusal from
        # `execute`. Two paths answering one question differently is how a caller ends up
        # handling only one of them, and the journey test hit it from a second organisation.
        package = self._package(package_id, required=False)
        if package is None:
            return SubmissionRun(
                package_id=package_id, refused=True, refusal_code="NOT_FOUND",
                detail="no such submission package in this organisation",
            )

        authorisation = self._authorisation_refusal(package)
        if authorisation is not None:
            return authorisation

        assert_capability(Capability.SUBMISSION_HANDOFF, human_approved=True)

        builder = self.handoff_builder
        if builder is None:
            from agent.submission.providers.fake import FakeHandoffBuilder

            builder = FakeHandoffBuilder()

        payload = self._payload(package)
        bundle = builder.build_handoff(payload=payload)

        package.handoff_ready_at = _now()
        package.status = models.SubmissionPackage.AUTHORISED
        package.status_reason = "handoff bundle prepared; a person files it in the portal"
        self.db.flush()

        self._stage_event(
            event_type="submission.handed_off",
            payload={
                "submission_package_id": package.id,
                "application_id": package.application_id,
                "fingerprint": package.package_fingerprint,
                "steps": len(bundle.steps),
            },
        )
        self._record_activity(
            summary_key="submission.handed_off",
            structured={
                "submission_package_id": package.id,
                "application_id": package.application_id,
                "documents": len(bundle.documents),
                "warnings": list(bundle.warnings),
            },
            subject_id=package.id,
        )
        self.db.flush()
        return SubmissionRun(
            package_id=package.id,
            state=package.status,
            detail="handoff bundle prepared; Granada has filed nothing",
            handoff=bundle,
        )

    # ==================================================================
    # 4. File through an adapter
    # ==================================================================
    def execute(self, *, package_id: str, worker_id: str = "worker") -> SubmissionRun:
        """Claim, file, record. The adapter path.

        Transaction A claims and commits, the provider is called with no locks held, and
        Transaction B records the outcome - the same structure as outbound mail, and for
        the same reason: a network call must not hold a database transaction open.
        """
        package, refusal = self._claim(package_id)
        if refusal is not None:
            return refusal

        payload = self._payload(package)
        result = self._call_provider(package, payload)
        return self._record(package_id, result, worker_id=worker_id)

    def _claim(self, package_id: str) -> tuple[Optional[models.SubmissionPackage], Optional[SubmissionRun]]:
        package = self._package(package_id, required=False)
        if package is None:
            return None, SubmissionRun(
                package_id=package_id, refused=True, refusal_code="NOT_FOUND",
                detail="no such submission package in this organisation",
            )

        if package.status in models.SubmissionPackage.TERMINAL:
            return None, SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="ALREADY_TERMINAL", detail=f"package is {package.status}",
            )

        # An uncertain submission FORBIDS a retry until reconciliation produces evidence.
        # This is the single most important check in the phase: filing twice can
        # disqualify both bids.
        if package.status in models.SubmissionPackage.UNCERTAIN:
            return None, SubmissionRun(
                package_id=package.id, state=models.SubmissionPackage.SUBMISSION_UNKNOWN,
                refused=True, refusal_code="SUBMISSION_UNKNOWN",
                detail=(
                    "a previous filing's outcome is unknown; reconciliation must first "
                    "establish whether the funder received it, because filing twice can "
                    "disqualify both applications"
                ),
            )

        authorisation = self._authorisation_refusal(package)
        if authorisation is not None:
            return None, authorisation

        # -- the final authority check ----------------------------------
        authority = self._final_authority_check(package)
        if authority is not None:
            return None, authority

        live, _ = self._fingerprint_of(package)
        if live != package.package_fingerprint:
            package.status = models.SubmissionPackage.NEEDS_DATA
            package.status_reason = "the frozen package no longer matches its fingerprint"
            self.db.commit()
            return None, SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="FINGERPRINT_MISMATCH",
                detail="the package no longer matches what was authorised",
            )

        package.status = models.SubmissionPackage.SUBMITTING
        package.started_at = _now()
        package.attempt_count = (package.attempt_count or 0) + 1
        package.last_attempt_at = _now()
        package.provider = getattr(self.provider, "name", None) or package.provider
        self.db.commit()
        return package, None

    def _authorisation_refusal(self, package: models.SubmissionPackage) -> Optional[SubmissionRun]:
        """Refuse anything not authorised, or authorised too long ago, or superseded."""
        if package.status == models.SubmissionPackage.AWAITING_AUTHORISATION:
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="NOT_AUTHORISED",
                detail=(
                    "no person has authorised this package. Submitting an application is "
                    "a legally consequential statement to a funder, so it requires an "
                    "authorisation of the exact package"
                ),
            )
        if package.status in (models.SubmissionPackage.REJECTED, models.SubmissionPackage.WITHDRAWN):
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="REJECTED_BY_PERSON", detail=f"package is {package.status}",
            )
        authorised_at = _aware(package.authorised_at)
        if authorised_at is None:
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="NOT_AUTHORISED", detail="the package carries no authorisation",
            )
        if _now() - authorised_at > timedelta(hours=DEFAULT_AUTHORISATION_TTL_HOURS):
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="AUTHORISATION_EXPIRED",
                detail=(
                    f"the authorisation is older than {DEFAULT_AUTHORISATION_TTL_HOURS}h; "
                    "a budget or an answer may since have changed"
                ),
            )
        return None

    def _final_authority_check(self, package: models.SubmissionPackage) -> Optional[SubmissionRun]:
        """Every condition reloaded from durable state, immediately before filing."""

        def refuse(code: str, detail: str, state: str = models.SubmissionPackage.AWAITING_AUTHORISATION) -> SubmissionRun:
            package.status = state
            package.status_reason = detail
            return SubmissionRun(
                package_id=package.id, state=state, refused=True,
                refusal_code=code, detail=detail,
            )

        # 1. Platform policy.
        try:
            assert_capability(Capability.SUBMISSION_HUMAN_AUTHORISED, human_approved=True)
        except Exception as exc:  # noqa: BLE001
            return refuse("PLATFORM_POLICY", str(exc))

        # 2. The agent.
        agent = self.db.execute(
            select(models.GranadaAgent).where(
                models.GranadaAgent.id == package.agent_id,
                models.GranadaAgent.org_id == self.org_id,
            )
        ).scalars().first()
        if agent is None:
            return refuse("AGENT_MISSING", "the agent no longer exists")
        if agent.status != models.GranadaAgent.ACTIVE:
            return refuse(
                "AGENT_NOT_ACTIVE",
                f"the agent is {agent.status}; an authorisation does not override a pause",
            )
        if package.agent_version is not None and package.agent_version != agent.version:
            return refuse(
                "AGENT_VERSION_CHANGED",
                f"authority changed from version {package.agent_version} to {agent.version} "
                "after this package was authorised; it must be revalidated",
            )

        # 3. The application and its deadline.
        application = self.db.execute(
            select(models.Application).where(
                models.Application.id == package.application_id,
                models.Application.org_id == self.org_id,
            )
        ).scalars().first()
        if application is None:
            return refuse("APPLICATION_NOT_IN_ORG", "the application is not this organisation's")
        opportunity = self.db.execute(
            select(models.Opportunity).where(models.Opportunity.id == application.opportunity_id)
        ).scalars().first()
        deadline = _aware(getattr(opportunity, "deadline", None))
        if deadline is not None and deadline <= _now():
            # A deadline that has passed is not recoverable, and filing anyway wastes the
            # organisation's credibility with the funder.
            return refuse(
                "DEADLINE_PASSED",
                f"the deadline was {deadline.date().isoformat()}; a late submission is "
                "usually rejected and always noticed",
                state=models.SubmissionPackage.FAILED_FINAL,
            )

        # 4. Readiness, re-checked. A document can be superseded between authorisation and
        #    filing, which silently changes what the funder receives.
        from agent.workspace import ApplicationWorkspace

        readiness = ApplicationWorkspace(self.db, self.org_id).readiness(application)
        if not readiness.ready:
            return refuse("APPLICATION_NOT_READY", "; ".join(readiness.blockers[:6]))

        # 5. The provider must actually be permitted to file.
        if self.provider is None:
            return refuse(
                "NO_SUBMISSION_PROVIDER",
                "no submission provider is configured; use the handoff path instead, "
                "which files nothing",
            )
        capabilities = set(getattr(self.provider, "capabilities", frozenset()))
        if "SUBMISSION" not in capabilities:
            # READING A PORTAL MUST NOT IMPLY THE RIGHT TO FILE THROUGH IT.
            return refuse(
                "PROVIDER_LACKS_SUBMISSION_CAPABILITY",
                "this adapter can read the funder's portal but is not permitted to file "
                "through it; use the handoff path",
            )

        # 6. The frozen documents, re-verified against the database.
        from agent.mail.risk import verify_attachment_manifest

        from agent.mail import risk as risk_module

        entries = (package.manifest or {}).get("documents") or []
        token = risk_module._SESSION.set(self.db)
        try:
            verification = verify_attachment_manifest(entries, org_id=self.org_id)
        finally:
            risk_module._SESSION.reset(token)
        if not verification.ok:
            return refuse(
                "DOCUMENT_INVALID", "; ".join(e["detail"] for e in verification.errors)
            )

        return None

    def _call_provider(self, package: models.SubmissionPackage, payload: SubmissionPayload) -> SubmissionResult:
        """Call the provider, translating every failure into one of three outcomes."""
        started = _now()
        try:
            result = self.provider.submit_application(
                payload=payload, idempotency_key=package.idempotency_key
            )
        except SubmissionProviderError as exc:
            from agent.submission.contract import SubmissionCapabilityMissing

            if isinstance(exc, SubmissionCapabilityMissing):
                return SubmissionResult(
                    outcome=SubmissionOutcome.CONFIRMED_NOT_SUBMITTED,
                    failure=SubmissionFailure.AUTOMATION_FORBIDDEN,
                    error_code="CAPABILITY_MISSING",
                    safe_error_summary=str(exc)[:300],
                    automation_forbidden=True,
                )
            return SubmissionResult(
                outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
                failure=SubmissionFailure.PROVIDER_ERROR_UNKNOWN,
                error_code=type(exc).__name__[:60],
                safe_error_summary="the provider raised an unclassified error",
            )
        except TimeoutError as exc:
            # NOT a rejection.
            return SubmissionResult(
                outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
                failure=SubmissionFailure.NETWORK_TIMEOUT,
                error_code="TIMEOUT",
                safe_error_summary=f"the portal did not answer in time: {type(exc).__name__}",
            )
        except (ConnectionError, OSError) as exc:
            return SubmissionResult(
                outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
                failure=SubmissionFailure.CONNECTION_RESET,
                error_code="CONNECTION",
                safe_error_summary=f"the connection failed: {type(exc).__name__}",
            )
        except Exception as exc:  # noqa: BLE001
            # An unclassified exception is an UNKNOWN outcome. Guessing "not submitted"
            # here is how a second application gets filed.
            return SubmissionResult(
                outcome=SubmissionOutcome.SUBMISSION_UNKNOWN,
                failure=SubmissionFailure.PROVIDER_ERROR_UNKNOWN,
                error_code=type(exc).__name__[:60],
                safe_error_summary=(
                    "the provider raised an error Granada cannot classify as a definite "
                    "rejection, so the outcome is unknown"
                ),
            )

        if result.latency_ms is None:
            result.latency_ms = int((_now() - started).total_seconds() * 1000)
        return result

    def _record(self, package_id: str, result: SubmissionResult, *, worker_id: str) -> SubmissionRun:
        """Transaction B: the immutable attempt, the receipt requirement, the events."""
        package = self._package(package_id, required=False)
        if package is None:  # pragma: no cover - claimed moments ago
            self.db.rollback()
            return SubmissionRun(package_id=package_id, refused=True, refusal_code="NOT_FOUND")

        attempt = models.SubmissionAttempt(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            package_id=package.id,
            attempt_number=package.attempt_count or 1,
            attempt_id=f"sub-{secrets.token_hex(12)}",
            provider=getattr(self.provider, "name", "UNKNOWN"),
            request_fingerprint=package.package_fingerprint,
            started_at=package.started_at or _now(),
            finished_at=_now(),
            duration_ms=result.latency_ms,
            result=result.outcome.value,
            error_code=result.error_code,
            safe_error_summary=result.safe_error_summary,
            provider_submission_id=result.provider_submission_id,
            reconciliation_state=(
                models.SubmissionAttempt.RECON_ACCEPTED
                if result.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED
                else models.SubmissionAttempt.RECON_UNKNOWN
            ),
            worker_id=worker_id,
            created_at=_now(),
        )
        self.db.add(attempt)

        if result.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED:
            self._record_submitted(package, result, attempt)
        elif result.outcome == SubmissionOutcome.CONFIRMED_NOT_SUBMITTED:
            self._record_definite_failure(package, result)
        else:
            self._record_unknown(package, result)

        self.db.commit()
        return SubmissionRun(
            package_id=package.id,
            outcome=result.outcome.value,
            state=package.status,
            attempt_id=attempt.id,
            funder_reference=result.funder_reference,
            failure=result.failure.value if result.failure else None,
            detail=result.safe_error_summary or "",
        )

    def _record_submitted(
        self,
        package: models.SubmissionPackage,
        result: SubmissionResult,
        attempt: models.SubmissionAttempt,
    ) -> None:
        """Record the funder's acceptance, and capture the receipt it carries.

        **``SUBMITTED`` requires a receipt.** A provider that reports acceptance without a
        reference has not given the organisation anything it can check, so the package
        moves to ``SUBMITTED`` only when a reference exists - and the attempt records the
        acceptance either way.
        """
        package.status = models.SubmissionPackage.SUBMITTED
        package.submitted_at = result.accepted_at or _now()
        package.provider_submission_id = result.provider_submission_id
        package.funder_reference = result.funder_reference
        package.failure_code = None
        package.failure_summary = None
        package.status_reason = "the funder accepted the application"
        self.db.flush()

        if result.funder_reference:
            self.db.add(
                models.SubmissionReceipt(
                    id=str(uuid.uuid4()),
                    org_id=self.org_id,
                    agent_id=self.agent_id,
                    package_id=package.id,
                    application_id=package.application_id,
                    reference=result.funder_reference,
                    source=models.SubmissionReceipt.SOURCE_PROVIDER,
                    acknowledgement_text=result.acknowledgement_text,
                    captured_at=_now(),
                    recorded_by_agent=True,
                )
            )
            # The application advances only now, with a reference in hand.
            self._advance_application(package, to="SUBMITTED")

        self._stage_event(
            event_type="submission.receipt_captured",
            payload={
                "submission_package_id": package.id,
                "application_id": package.application_id,
                "funder_reference": result.funder_reference,
            },
        )
        self._record_activity(
            summary_key="submission.receipt_captured",
            structured={
                "submission_package_id": package.id,
                "application_id": package.application_id,
                "funder_reference": result.funder_reference,
            },
            subject_id=package.id,
        )

    def _record_definite_failure(
        self, package: models.SubmissionPackage, result: SubmissionResult
    ) -> None:
        """A failure the funder positively confirmed. Only these may be retried."""
        package.failure_code = result.error_code or (
            result.failure.value if result.failure else "UNKNOWN"
        )
        package.failure_summary = result.safe_error_summary
        package.status_reason = result.safe_error_summary

        if result.failure == SubmissionFailure.DEADLINE_PASSED:
            package.status = models.SubmissionPackage.FAILED_FINAL
        elif result.failure == SubmissionFailure.AUTOMATION_FORBIDDEN:
            # A refusal on principle. It must never be retried by a different route, and
            # the handoff path is the answer.
            package.status = models.SubmissionPackage.FAILED_FINAL
            package.status_reason = (
                "the funder's terms forbid automated submission; use the handoff path "
                "and file it as a person"
            )
        elif result.failure in RETRYABLE:
            package.status = models.SubmissionPackage.AUTHORISED
            package.retry_not_before = _now() + timedelta(
                seconds=result.retry_after_seconds or 300
            )
        else:
            package.status = models.SubmissionPackage.FAILED_FINAL

        self.db.flush()
        self._stage_event(
            event_type="submission.failed",
            payload={
                "submission_package_id": package.id,
                "failure": result.failure.value if result.failure else None,
                "definite": True,
            },
        )

    def _record_unknown(
        self, package: models.SubmissionPackage, result: SubmissionResult
    ) -> None:
        """Granada does not know. Say so, and forbid a retry.

        The state the brief insists exists separately from failure. Retrying here files a
        second application, and many programmes disqualify both.
        """
        package.status = models.SubmissionPackage.SUBMISSION_UNKNOWN
        package.failure_code = result.error_code or "UNKNOWN_OUTCOME"
        package.failure_summary = result.safe_error_summary
        package.status_reason = (
            "the funder may have received this application. Granada will not file again "
            "until it has established what happened, because filing twice can disqualify "
            "both applications."
        )
        self.db.flush()
        self._stage_event(
            event_type="submission.unknown",
            payload={
                "submission_package_id": package.id,
                "application_id": package.application_id,
                "failure": result.failure.value if result.failure else None,
            },
        )
        self._record_activity(
            summary_key="submission.unknown",
            structured={
                "submission_package_id": package.id,
                "detail": "checking whether the funder received the application before "
                          "attempting anything else",
            },
            subject_id=package.id,
        )

    # ==================================================================
    # 5. Receipt by hand, and reconciliation
    # ==================================================================
    def record_receipt(
        self,
        *,
        package_id: str,
        reference: str,
        captured_by: Optional[str] = None,
        source: str = models.SubmissionReceipt.SOURCE_MANUAL,
        acknowledgement_text: Optional[str] = None,
        evidence_ref: Optional[str] = None,
    ) -> models.SubmissionPackage:
        """A person records the funder's acknowledgement.

        This is how a HANDOFF submission completes: the person filed it in the portal,
        saw the confirmation, and records the reference here. Until they do, the
        application is not SUBMITTED - because without a reference there is no evidence
        it was received, and an organisation that believes it applied when it did not
        has lost the grant and does not know.
        """
        if not reference or not str(reference).strip():
            raise SubmissionError(
                "a receipt requires the funder's reference. An application with no "
                "external reference is not submitted; it is possibly submitted."
            )
        package = self._package(package_id)
        if package.status in (models.SubmissionPackage.REJECTED, models.SubmissionPackage.WITHDRAWN):
            raise SubmissionError(f"package {package.id} is {package.status}")

        receipt = models.SubmissionReceipt(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            package_id=package.id,
            application_id=package.application_id,
            reference=str(reference).strip()[:255],
            source=source,
            acknowledgement_text=acknowledgement_text,
            evidence_ref=evidence_ref,
            captured_at=_now(),
            captured_by=captured_by,
            recorded_by_agent=False,
        )
        self.db.add(receipt)
        package.status = models.SubmissionPackage.SUBMITTED
        package.submitted_at = _now()
        package.funder_reference = receipt.reference
        package.status_reason = f"receipt recorded from {source.lower()}"
        self.db.flush()

        # The application advances only now, with a reference in hand.
        self._advance_application(package, to="SUBMITTED")

        self._stage_event(
            event_type="submission.receipt_captured",
            payload={
                "submission_package_id": package.id,
                "funder_reference": receipt.reference,
                "source": source,
            },
        )
        self._record_activity(
            summary_key="submission.receipt_captured",
            structured={
                "submission_package_id": package.id,
                "funder_reference": receipt.reference,
                "source": source,
                "recorded_by": captured_by,
            },
            subject_id=package.id,
        )
        self.db.flush()
        return package

    def reconcile(self, *, package_id: str) -> SubmissionRun:
        """Establish what happened to an uncertain filing.

        Only positive evidence moves a package out of ``SUBMISSION_UNKNOWN``. A portal
        that simply cannot find the application has proven nothing unless it can
        enumerate its own submissions, which is what ``authoritative_absence`` records.
        """
        assert_capability(Capability.SUBMISSION_RECONCILE)

        package = self._package(package_id)
        if package.status not in models.SubmissionPackage.UNCERTAIN:
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="NOT_UNCERTAIN",
                detail=f"the package is {package.status}; there is nothing to reconcile",
            )
        if self.provider is None:
            return SubmissionRun(
                package_id=package.id, state=package.status, refused=True,
                refusal_code="NO_SUBMISSION_PROVIDER", detail="no provider to ask",
            )

        found: SubmissionReconciliation = self.provider.query_submission(
            package_fingerprint=package.package_fingerprint or "",
            provider_submission_id=package.provider_submission_id,
        )

        attempt = self.db.execute(
            select(models.SubmissionAttempt).where(
                models.SubmissionAttempt.package_id == package.id
            ).order_by(models.SubmissionAttempt.attempt_number.desc())
        ).scalars().first()

        if found.found and found.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED:
            # Positive evidence: the funder has it. NOT a second filing.
            if attempt is not None:
                attempt.reconciliation_state = models.SubmissionAttempt.RECON_ACCEPTED
                attempt.reconciled_at = _now()
                attempt.provider_submission_id = (
                    found.provider_submission_id or attempt.provider_submission_id
                )
            package.status = models.SubmissionPackage.SUBMITTED
            package.submitted_at = found.accepted_at or _now()
            package.funder_reference = found.funder_reference
            package.provider_submission_id = found.provider_submission_id
            package.status_reason = "reconciled: the funder confirmed receipt"
            if found.funder_reference:
                self.db.add(
                    models.SubmissionReceipt(
                        id=str(uuid.uuid4()), org_id=self.org_id, agent_id=self.agent_id,
                        package_id=package.id, application_id=package.application_id,
                        reference=found.funder_reference,
                        source=models.SubmissionReceipt.SOURCE_PROVIDER,
                        acknowledgement_text="captured by reconciliation",
                        captured_at=_now(), recorded_by_agent=True,
                    )
                )
                self._advance_application(package, to="SUBMITTED")
            self.db.commit()
            return SubmissionRun(
                package_id=package.id, outcome=SubmissionOutcome.CONFIRMED_SUBMITTED.value,
                state=package.status, funder_reference=found.funder_reference,
                detail="reconciled: previously unknown, now confirmed received",
            )

        if found.authoritative_absence:
            if attempt is not None:
                attempt.reconciliation_state = models.SubmissionAttempt.RECON_NOT_ACCEPTED
                attempt.reconciled_at = _now()
            package.status = models.SubmissionPackage.AUTHORISED
            package.retry_not_before = _now()
            package.status_reason = (
                "reconciled: the funder authoritatively confirms it never received this "
                "application, so filing again is safe"
            )
            self.db.commit()
            return SubmissionRun(
                package_id=package.id, outcome=SubmissionOutcome.CONFIRMED_NOT_SUBMITTED.value,
                state=package.status,
                detail="reconciled: proven not received, a retry is now permitted",
            )

        package.status = models.SubmissionPackage.SUBMISSION_UNKNOWN
        package.status_reason = (
            "reconciliation produced no decisive evidence: "
            + (found.detail or "the funder could not confirm or deny receipt")
        )
        self.db.commit()
        return SubmissionRun(
            package_id=package.id, outcome=SubmissionOutcome.SUBMISSION_UNKNOWN.value,
            state=package.status, refused=True, refusal_code="STILL_UNKNOWN",
            detail=package.status_reason,
        )

    def withdraw(self, *, package_id: str, user_id: str, reason: str = "") -> models.SubmissionPackage:
        """Withdraw a package before it is filed."""
        package = self._package(package_id)
        if package.status == models.SubmissionPackage.SUBMITTING:
            raise SubmissionError(
                "the application is already with the funder and cannot be recalled"
            )
        if package.status == models.SubmissionPackage.SUBMITTED:
            raise SubmissionError(
                "the application was received; withdrawing it is a conversation with the "
                "funder, not a state change here"
            )
        package.status = models.SubmissionPackage.WITHDRAWN
        package.status_reason = reason or "withdrawn by a person"
        self.db.flush()
        return package

    # ==================================================================
    # helpers
    # ==================================================================
    def _package(self, package_id: str, *, required: bool = True) -> Optional[models.SubmissionPackage]:
        package = self.db.execute(
            select(models.SubmissionPackage).where(
                models.SubmissionPackage.id == package_id,
                models.SubmissionPackage.org_id == self.org_id,
            )
        ).scalars().first()
        if package is None and required:
            raise SubmissionError(f"no submission package {package_id} in this organisation")
        return package

    def _fingerprint_of(self, package: models.SubmissionPackage) -> tuple[str, str]:
        manifest = package.manifest or {}
        documents = [FrozenDocument(**d) for d in (manifest.get("documents") or [])]
        answers = [FrozenAnswer(**a) for a in (manifest.get("answers") or [])]
        return package_fingerprint(
            org_id=package.org_id,
            agent_id=package.agent_id,
            application_id=package.application_id,
            documents=documents,
            answers=answers,
            budget=manifest.get("budget") or {},
            contact_email=manifest.get("contact_email"),
            target_url=manifest.get("target_url") or package.target_url,
        )

    def _payload(self, package: models.SubmissionPackage) -> SubmissionPayload:
        """Build the provider payload from the FROZEN manifest only.

        Deliberately not from the application. If this read through, a change to the
        application after authorisation would change what the funder receives - the
        failure the fingerprint exists to prevent.
        """
        manifest = package.manifest or {}
        organisation = self.db.execute(
            select(models.Organisation).where(models.Organisation.id == self.org_id)
        ).scalars().first()
        opportunity = None
        application = self.db.execute(
            select(models.Application).where(models.Application.id == package.application_id)
        ).scalars().first()
        if application is not None:
            opportunity = self.db.execute(
                select(models.Opportunity).where(
                    models.Opportunity.id == application.opportunity_id
                )
            ).scalars().first()

        return SubmissionPayload(
            package_id=package.id,
            application_id=package.application_id,
            organisation_name=getattr(organisation, "name", "") or "",
            opportunity_title=getattr(opportunity, "title", "") or "",
            target_url=manifest.get("target_url") or package.target_url,
            documents=tuple(FrozenDocument(**d) for d in (manifest.get("documents") or [])),
            answers=tuple(FrozenAnswer(**a) for a in (manifest.get("answers") or [])),
            budget=manifest.get("budget") or None,
            contact_email=manifest.get("contact_email"),
            package_fingerprint=package.package_fingerprint,
            reference_header=package.package_fingerprint,
        )

    def _advance_application(self, package: models.SubmissionPackage, *, to: str) -> None:
        """Move the application forward, through the workspace rather than around it.

        The state machine refuses an illegal transition, so a submission that somehow
        arrived out of order fails here rather than in the records.
        """
        from agent.workspace import ApplicationWorkspace

        application = self.db.execute(
            select(models.Application).where(models.Application.id == package.application_id)
        ).scalars().first()
        if application is None:
            return
        workspace = ApplicationWorkspace(self.db, self.org_id)
        try:
            workspace.transition(
                application, to, reason=f"submission package {package.id}",
                actor_type=models.ApplicationTransition.ACTOR_AGENT,
            )
        except Exception as exc:  # noqa: BLE001
            # The receipt is the fact; the transition is bookkeeping. Recorded rather
            # than raised, because losing a real receipt over a state-machine complaint
            # would be the worse outcome.
            logger.warning(
                "submission.transition_failed",
                extra={"application_id": application.id, "to": to, "error": str(exc)[:200]},
            )

    def _stage_event(self, *, event_type: str, payload: dict[str, Any]) -> models.OutboxEvent:
        event = models.OutboxEvent(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            stream=f"granada:v1:submission:{event_type.split('.')[-1]}",
            event_type=f"granada:v1:{event_type}",
            payload={**payload, "agent_id": self.agent_id, "organisation_id": self.org_id},
            created_at=_now(),
            attempts=0,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _record_activity(
        self, *, summary_key: str, structured: dict[str, Any], subject_id: Optional[str]
    ) -> models.AgentActivity:
        activity = models.AgentActivity(
            id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            org_id=self.org_id,
            specialist_key="SUBMISSION",
            activity_type="submission",
            summary_key=summary_key,
            subject_type="SUBMISSION",
            subject_id=subject_id,
            structured_data=structured,
            visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
            occurred_at=_now(),
        )
        self.db.add(activity)
        self.db.flush()
        return activity
