"""Organisation Memory: the Digital Twin and the document vault.

The rule this module exists to enforce
--------------------------------------
**An AI-inferred fact must never silently become a fact in a submission.**

That is the failure the security gate names explicitly, and it is a *silent*
failure by nature: nothing errors, a plausible number appears where a real one
should be, and a funder receives an application asserting something the
organisation never confirmed. It is also unrecoverable - a submitted application
cannot be un-submitted.

So the boundary is drawn here, in one function, and nothing bypasses it:
``submission_facts()`` returns **only** states a human or a trusted source
stands behind. ``AI_INFERRED`` is excluded, and it is excluded by an allow-list
rather than a deny-list, so adding a new state in future does not silently make
it submission-safe.

Missing facts are the other half
--------------------------------
The brief forbids fabricating organisational facts: *"missing material facts
become user tasks."* ``missing_facts()`` is therefore a first-class operation,
not an error path. A system that can only fail on missing data will eventually
be tempted to guess; a system that can *ask* will not.

Versioning
----------
Facts and documents are appended, never overwritten. Superseding sets
``is_current = False`` on the old version and inserts a new one. An application
submitted against version 3 stays explainable after version 4 arrives, which is
what makes the required "Why?" view honest rather than reconstructed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


class OrganisationMemoryError(RuntimeError):
    """Base class for memory failures."""


class UnknownFactState(OrganisationMemoryError):
    """A state outside the closed set was supplied."""


class FabricationRefused(OrganisationMemoryError):
    """Something tried to make an unverified value usable in a submission.

    Raised rather than logged, because a caller that catches this and continues
    is doing exactly what the rule forbids, and the traceback should say so.
    """


# The closed set. Deliberately not an Enum member comparison everywhere: these
# strings are stored, queried and reported, so they are the schema's vocabulary.
ALL_STATES = frozenset(
    {
        models.OrgFact.VERIFIED,
        models.OrgFact.USER_PROVIDED,
        models.OrgFact.IMPORTED,
        models.OrgFact.AI_INFERRED,
        models.OrgFact.EXPIRED,
    }
)

# An ALLOW-list, not a deny-list. If a new state is added later and someone
# forgets to think about whether it is submission-safe, the default is unsafe -
# which is the correct direction for this particular mistake.
SUBMISSION_SAFE_STATES = frozenset(
    {
        models.OrgFact.VERIFIED,
        models.OrgFact.USER_PROVIDED,
        models.OrgFact.IMPORTED,
    }
)

# Which states a human can promote a fact to, and from where. AI_INFERRED -> a
# safe state is allowed, because a human confirming a guess is exactly how a
# guess becomes a fact; it just must be a deliberate act by a named person.
_PROMOTABLE_FROM = {
    models.OrgFact.AI_INFERRED: SUBMISSION_SAFE_STATES,
    models.OrgFact.IMPORTED: SUBMISSION_SAFE_STATES,
    models.OrgFact.USER_PROVIDED: SUBMISSION_SAFE_STATES,
    models.OrgFact.VERIFIED: SUBMISSION_SAFE_STATES,
    models.OrgFact.EXPIRED: SUBMISSION_SAFE_STATES,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a possibly-naive timestamp.

    ``DateTime(timezone=True)`` is aware from PostgreSQL but naive from SQLite,
    so a value that has round-tripped through the database can differ in
    awareness from a fresh ``now()``. Comparing the two raises ``TypeError``.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass(frozen=True)
class MissingFact:
    """A material fact the organisation has not supplied.

    This is a work item for a human, not an error. The brief's rule is that a
    missing material fact becomes a user task rather than something the system
    invents.
    """

    key: str
    reason: str
    blocking: bool = True


class OrganisationMemory:
    """Reads and writes the Digital Twin for one tenant."""

    def __init__(self, db: Session, org_id: str, *, actor_id: Optional[str] = None) -> None:
        if not org_id:
            # "Tenant unknown" is a deny, never a default tenant. Constructing
            # memory for an unnamed tenant would be the first step in reading
            # somebody else's organisation.
            raise OrganisationMemoryError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------
    def record_fact(
        self,
        *,
        key: str,
        value: Any,
        state: str,
        source: str,
        value_type: str = "text",
        confidence: Optional[float] = None,
        source_ref: Optional[str] = None,
        evidence_document_id: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
    ) -> models.OrgFact:
        """Append a new version of a fact, superseding the current one.

        ``state`` and ``source`` are required and validated here rather than by
        the database alone, because the useful error message is "AI_INFERRED
        facts cannot be submitted" or "a fact needs a source", not a constraint
        violation naming a column.
        """
        if state not in ALL_STATES:
            raise UnknownFactState(
                f"state must be one of {sorted(ALL_STATES)}, got {state!r}"
            )
        if not source or not str(source).strip():
            raise OrganisationMemoryError(
                f"fact {key!r} has no source; a fact whose provenance is unknown "
                "cannot be stored"
            )
        if not key or not key.strip():
            raise OrganisationMemoryError("fact key is required")
        if state == models.OrgFact.AI_INFERRED and confidence is None:
            # Not a hard requirement, but an inference without a stated
            # confidence is indistinguishable from a stated fact in a review UI,
            # and the review UI is the only thing standing between it and a
            # submission.
            logger.warning(
                "ai_inferred_fact_without_confidence",
                extra={"org_id": self.org_id, "key": key},
            )

        current = self.current_fact(key)
        version = (current.version + 1) if current else 1

        if current is not None:
            current.is_current = False
            current.supersedes_id = None

        fact = models.OrgFact(
            org_id=self.org_id,
            key=key,
            value={"value": value} if not isinstance(value, dict) else value,
            value_type=value_type,
            state=state,
            confidence=confidence,
            source=source,
            source_ref=source_ref,
            evidence_document_id=evidence_document_id,
            version=version,
            is_current=True,
            supersedes_id=current.id if current else None,
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=_now(),
        )
        self.db.add(fact)
        self.db.flush()
        return fact

    def current_fact(self, key: str) -> Optional[models.OrgFact]:
        return self.db.execute(
            select(models.OrgFact)
            .where(
                models.OrgFact.org_id == self.org_id,
                models.OrgFact.key == key,
                models.OrgFact.is_current.is_(True),
            )
            .order_by(models.OrgFact.version.desc())
        ).scalars().first()

    def fact_history(self, key: str) -> list[models.OrgFact]:
        """Every version, oldest first - the evidence trail for one fact."""
        return list(
            self.db.execute(
                select(models.OrgFact)
                .where(models.OrgFact.org_id == self.org_id, models.OrgFact.key == key)
                .order_by(models.OrgFact.version.asc())
            ).scalars()
        )

    def current_facts(self) -> list[models.OrgFact]:
        return list(
            self.db.execute(
                select(models.OrgFact)
                .where(
                    models.OrgFact.org_id == self.org_id,
                    models.OrgFact.is_current.is_(True),
                )
                .order_by(models.OrgFact.key.asc())
            ).scalars()
        )

    def is_expired(self, fact: models.OrgFact, *, now: Optional[datetime] = None) -> bool:
        moment = now or _now()
        if fact.state == models.OrgFact.EXPIRED:
            return True
        expiry = _aware(fact.valid_until)
        return expiry is not None and expiry <= moment

    def verify_fact(
        self,
        *,
        key: str,
        verified_by: str,
        value: Any = None,
        evidence_document_id: Optional[str] = None,
    ) -> models.OrgFact:
        """Promote a fact to ``VERIFIED`` on the authority of a named person.

        The only route by which an ``AI_INFERRED`` value can become usable. It
        requires a human id, because "the system decided it was fine" is not
        verification.
        """
        if not verified_by:
            raise OrganisationMemoryError(
                "verification requires a named person; an unattributed "
                "verification is not one"
            )
        current = self.current_fact(key)
        if current is None:
            raise OrganisationMemoryError(f"no fact {key!r} to verify")
        if value is None:
            value = (current.value or {}).get("value", current.value)

        promoted = self.record_fact(
            key=key,
            value=value,
            state=models.OrgFact.VERIFIED,
            source=f"user:{verified_by}",
            value_type=current.value_type,
            evidence_document_id=evidence_document_id or current.evidence_document_id,
            valid_from=current.valid_from,
            valid_until=current.valid_until,
        )
        promoted.verified_at = _now()
        promoted.verified_by = verified_by
        self.db.flush()
        return promoted

    def expire_stale_facts(self, *, now: Optional[datetime] = None) -> list[str]:
        """Mark lapsed facts ``EXPIRED`` and report which ones.

        Called before reading rather than by a nightly job alone, so a lapsed
        certificate cannot be used in the window between expiry and the sweep.
        """
        moment = now or _now()
        expired: list[str] = []
        for fact in self.current_facts():
            if fact.state == models.OrgFact.EXPIRED:
                continue
            expiry = _aware(fact.valid_until)
            if expiry is not None and expiry <= moment:
                fact.state = models.OrgFact.EXPIRED
                expired.append(fact.key)
        if expired:
            self.db.flush()
        return expired

    # ------------------------------------------------------------------
    # The submission boundary
    # ------------------------------------------------------------------
    def submission_facts(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """Facts that may be used in a submitted application. Nothing else.

        An allow-list over states, plus an expiry check. ``AI_INFERRED`` can
        never appear in the result, and neither can anything added to
        ``ALL_STATES`` later without someone deliberately adding it to
        ``SUBMISSION_SAFE_STATES``.
        """
        moment = now or _now()
        out: dict[str, Any] = {}
        excluded: list[str] = []
        for fact in self.current_facts():
            if fact.state not in SUBMISSION_SAFE_STATES:
                excluded.append(f"{fact.key}:{fact.state}")
                continue
            if self.is_expired(fact, now=moment):
                excluded.append(f"{fact.key}:EXPIRED")
                continue
            value = fact.value
            out[fact.key] = value.get("value") if isinstance(value, dict) and "value" in value else value

        if excluded:
            # Logged, not raised: the caller gets a usable mapping and the
            # exclusions are visible. What must never happen is an excluded fact
            # appearing in the result.
            logger.info(
                "submission_facts_excluded",
                extra={"org_id": self.org_id, "excluded": ",".join(sorted(excluded))},
            )
        return out

    def assert_submission_safe(self, key: str) -> models.OrgFact:
        """Return the current fact, or refuse because it is not usable.

        The loud version of :meth:`submission_facts`, for a single fact an agent
        is about to put into a document. Raises ``FabricationRefused`` rather
        than returning ``None`` so the failure lands at the point of use.
        """
        fact = self.current_fact(key)
        if fact is None:
            raise FabricationRefused(
                f"{key!r} is not known for this organisation; a missing material "
                "fact becomes a user task, never a guess"
            )
        if fact.state not in SUBMISSION_SAFE_STATES:
            raise FabricationRefused(
                f"{key!r} is {fact.state} and may not be submitted; only "
                f"{sorted(SUBMISSION_SAFE_STATES)} are usable"
            )
        if self.is_expired(fact):
            raise FabricationRefused(f"{key!r} has expired and may not be submitted")
        return fact

    def missing_facts(
        self, required: Iterable[str], *, now: Optional[datetime] = None
    ) -> list[MissingFact]:
        """Which required facts the organisation cannot yet support.

        Returns work items rather than raising, because the correct response to
        a missing fact is to ask the organisation - and a caller that can only
        receive an exception will eventually be tempted to fill the gap itself.
        """
        moment = now or _now()
        missing: list[MissingFact] = []
        for key in required:
            fact = self.current_fact(key)
            if fact is None:
                missing.append(MissingFact(key=key, reason="not provided"))
            elif fact.state not in SUBMISSION_SAFE_STATES:
                missing.append(
                    MissingFact(key=key, reason=f"{fact.state} - needs human confirmation")
                )
            elif self.is_expired(fact, now=moment):
                missing.append(MissingFact(key=key, reason="expired"))
        return missing

    def inferred_facts(self) -> list[models.OrgFact]:
        """Current AI inferences awaiting confirmation.

        Exposed so a review screen can show what the platform believes without
        being told, which is the only moment at which a wrong inference is cheap
        to correct.
        """
        return [
            fact
            for fact in self.current_facts()
            if fact.state == models.OrgFact.AI_INFERRED
        ]


# ---------------------------------------------------------------------------
# Document vault
# ---------------------------------------------------------------------------
def checksum_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class DocumentVault:
    """Versioned, checksummed, expiring, approval-gated documents."""

    def __init__(self, db: Session, org_id: str, *, actor_id: Optional[str] = None) -> None:
        if not org_id:
            raise OrganisationMemoryError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id
        self.actor_id = actor_id

    def add_version(
        self,
        *,
        title: str,
        doc_type: str,
        storage_key: str,
        checksum_sha256: str,
        mime_type: str,
        size_bytes: int = 0,
        scope: str = models.Document.SCOPE_ORGANISATION,
        scope_ref: Optional[str] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        uploaded_by: Optional[str] = None,
    ) -> models.Document:
        """Append a version. A new version is ``PENDING``, never pre-approved.

        Inheriting the previous version's approval would be the wrong default in
        the dangerous direction: the new file has different bytes, and it was the
        bytes that were approved.
        """
        if scope not in {
            models.Document.SCOPE_ORGANISATION,
            models.Document.SCOPE_PROJECT,
            models.Document.SCOPE_GRANT,
        }:
            raise OrganisationMemoryError(f"unknown document scope {scope!r}")
        if scope != models.Document.SCOPE_ORGANISATION and not scope_ref:
            raise OrganisationMemoryError(
                f"scope {scope} requires scope_ref; a project document that "
                "cannot say which project is not attachable"
            )
        if not checksum_sha256 or len(checksum_sha256) != 64:
            raise OrganisationMemoryError(
                "a document needs a sha256 checksum; a receipt naming a document "
                "id is only meaningful if the bytes cannot change"
            )

        existing = self.db.execute(
            select(models.Document).where(
                models.Document.org_id == self.org_id,
                models.Document.storage_key == storage_key,
            )
        ).scalars().all()

        superseded = [d for d in existing if d.is_current]
        version = max((d.version for d in existing), default=0) + 1
        for old in superseded:
            old.is_current = False

        document = models.Document(
            org_id=self.org_id,
            title=title,
            doc_type=doc_type,
            scope=scope,
            scope_ref=scope_ref,
            storage_key=storage_key,
            checksum_sha256=checksum_sha256,
            mime_type=mime_type,
            size_bytes=size_bytes,
            version=version,
            is_current=True,
            supersedes_id=superseded[0].id if superseded else None,
            valid_from=valid_from,
            valid_until=valid_until,
            approval_status=models.Document.PENDING,
            uploaded_by=uploaded_by or self.actor_id,
            created_at=_now(),
        )
        self.db.add(document)
        self.db.flush()
        return document

    def approve(self, document: models.Document, *, approved_by: str) -> models.Document:
        if not approved_by:
            raise OrganisationMemoryError("approval requires a named person")
        if document.org_id != self.org_id:
            raise OrganisationMemoryError("document belongs to another organisation")
        if not document.is_current:
            # Approving a superseded version would approve bytes that are no
            # longer the current file - a silent mismatch between what was
            # approved and what would be attached.
            raise OrganisationMemoryError(
                f"document {document.id} is version {document.version} and no "
                "longer current; approve the current version"
            )
        if not document.checksum_sha256:
            raise OrganisationMemoryError("refusing to approve a document with no checksum")
        document.approval_status = models.Document.APPROVED
        document.approved_by = approved_by
        document.approved_at = _now()
        self.db.flush()
        return document

    def reject(self, document: models.Document, *, rejected_by: str) -> models.Document:
        if not rejected_by:
            raise OrganisationMemoryError("rejection requires a named person")
        if document.org_id != self.org_id:
            raise OrganisationMemoryError("document belongs to another organisation")
        document.approval_status = models.Document.REJECTED
        document.approved_by = rejected_by
        document.approved_at = _now()
        self.db.flush()
        return document

    def usable(
        self,
        *,
        doc_type: Optional[str] = None,
        scope: Optional[str] = None,
        scope_ref: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[models.Document]:
        """Documents that may be attached to a submission right now.

        Current, approved, and not expired. Uploading is not approving, and the
        gap between those two is where the wrong document gets attached to a
        real application.
        """
        moment = now or _now()
        stmt = select(models.Document).where(
            models.Document.org_id == self.org_id,
            models.Document.is_current.is_(True),
            models.Document.approval_status == models.Document.APPROVED,
        )
        if doc_type:
            stmt = stmt.where(models.Document.doc_type == doc_type)
        if scope:
            stmt = stmt.where(models.Document.scope == scope)
        if scope_ref:
            stmt = stmt.where(models.Document.scope_ref == scope_ref)

        usable: list[models.Document] = []
        for document in self.db.execute(stmt).scalars():
            expiry = _aware(document.valid_until)
            if expiry is not None and expiry <= moment:
                continue
            usable.append(document)
        return usable

    def expiring_soon(self, *, days: int = 30, now: Optional[datetime] = None) -> list[models.Document]:
        """Approved documents about to lapse.

        The point of the vault knowing about expiry is to warn before a
        submission goes out with a certificate that expires next week.
        """
        moment = now or _now()
        horizon = moment + timedelta(days=days)
        out: list[models.Document] = []
        for document in self.usable(now=moment):
            expiry = _aware(document.valid_until)
            if expiry is not None and expiry <= horizon:
                out.append(document)
        return out

    def assert_usable(self, document_id: str, *, now: Optional[datetime] = None) -> models.Document:
        """Return the document, or refuse. The loud single-document form."""
        document = self.db.execute(
            select(models.Document).where(
                models.Document.id == document_id,
                models.Document.org_id == self.org_id,
            )
        ).scalars().first()
        if document is None:
            raise OrganisationMemoryError(f"no document {document_id} for this organisation")
        if not document.is_current:
            raise OrganisationMemoryError(
                f"document {document_id} is version {document.version} and been superseded"
            )
        if document.approval_status != models.Document.APPROVED:
            raise OrganisationMemoryError(
                f"document {document_id} is {document.approval_status}; uploading is "
                "not approving"
            )
        expiry = _aware(document.valid_until)
        if expiry is not None and expiry <= (now or _now()):
            raise OrganisationMemoryError(f"document {document_id} has expired")
        return document
