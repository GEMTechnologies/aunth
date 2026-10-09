"""Application package assembly: the frozen artefact set a human authorises.

WHAT WAS MISSING
----------------
`SubmissionPackage` has existed since Phase 8 with everything needed - `package_fingerprint`,
`manifest`, `idempotency_key`, `submission_mode`, `handoff_ready_at` - and its own docstring explains
the design:

    "A submission is not 'submit application 12': it is a specific set of documents at specific
     versions, specific answers to specific questions, and a specific budget. Freeze the fingerprint,
     and a human authorises THAT."

And **nothing created one.** `SubmissionPackage(` appears in `models.py` and nowhere else. So the
pipeline reached prepared documents and stopped, with the table that describes the thing a funder
receives sitting empty.

This module is the assembler. It creates no new tables and no new infrastructure.

THE FINGERPRINT IS THE WHOLE MECHANISM
--------------------------------------
`package_fingerprint` is computed from the INPUTS - the application version, the opportunity, and the
identity and checksum of every document included. Two consequences, both load-bearing:

* **Idempotent.** A retry, or a second worker racing the first, produces the same fingerprint and
  therefore the same package row. Re-assembling unchanged inputs is a no-op rather than a new
  revision.
* **Authorisation does not transfer.** Change a document and the fingerprint changes, so a previously
  authorised package no longer matches what is on disk. That is the point: a human authorised *that
  set*, and a different set needs a different authorisation.

MODE_HANDOFF BY DEFAULT
-----------------------
`MODE_HANDOFF` prepares everything and a person submits it in the funder's own portal. It performs no
external action, which is why the model's own comment calls it the fully-implemented mode. The
assembler defaults to it and nothing here submits anything.

A MISSING DOCUMENT IS NOT A FAILURE
-----------------------------------
The status `NEEDS_DATA` means the organisation has not supplied something. That is not an
infrastructure error and must never be reported as one - a registration certificate the NGO has not
uploaded is the organisation's outstanding action, and the operator message says so.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import models


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Documents the organisation must UPLOAD. The assembler cannot produce these, so their absence is
#: `NEEDS_DATA` for the organisation rather than a failure of this engine.
EVIDENCE_TYPES = frozenset(
    {"registration_certificate", "audited_accounts", "tax_clearance", "bank_details"}
)


@dataclass
class PackageItem:
    """One document in the package, frozen at a version."""

    document_id: str
    doc_type: str
    title: str
    version: int
    checksum_sha256: str
    storage_key: str
    mime_type: str

    def as_manifest_entry(self) -> dict[str, Any]:
        """What the manifest records about a document.

        NOT the storage path beyond what a download needs, and never the contents. A manifest is
        copied into audit records and exposed to an operator; a path is operational detail and a
        checksum is the identity that matters.
        """
        return {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "title": self.title,
            "version": self.version,
            "checksum_sha256": self.checksum_sha256,
            "mime_type": self.mime_type,
        }


@dataclass
class AssemblyResult:
    """What one assembly produced, and why."""

    status: str
    fingerprint: str
    manifest: dict[str, Any]
    included: list[PackageItem] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Requirements the funder states and the organisation must act on, distinct from technical
    #: failure. Reported separately all the way to the operator message.
    needs_data: list[str] = field(default_factory=list)
    reused: bool = False
    reason: str = ""


def fingerprint_of(
    *, org_id: str, opportunity_id: str, application_version: int, items: list[PackageItem]
) -> tuple[str, str]:
    """The deterministic identity of a package, and the text it was computed from.

    Sorted by document id so the fingerprint does not depend on the order the vault happened to
    return rows in - an unordered fingerprint would produce a new revision whenever the database
    chose a different plan, which is the sort of bug that only appears under load.

    The input text is returned as well as the digest, because "why is this a new revision" is
    otherwise unanswerable and the answer is usually one changed checksum.
    """
    parts = [
        f"org={org_id}",
        f"opportunity={opportunity_id}",
        f"application_version={application_version}",
    ]
    for item in sorted(items, key=lambda i: i.document_id):
        parts.append(f"{item.doc_type}:{item.version}:{item.checksum_sha256}")
    text = "\n".join(parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


def build_manifest(
    *,
    organisation: models.Organisation,
    opportunity: models.Opportunity,
    application: models.Application,
    items: list[PackageItem],
    missing: list[str],
    needs_data: list[str],
    status: str,
    assembled_at: datetime,
) -> dict[str, Any]:
    """The package manifest: what is in it, what is not, and what it is for.

    Deliberately not secret-bearing. It names the organisation and the opportunity, lists documents
    by type and checksum, and states the readiness - no document contents, no credentials, no
    contact details beyond what identifies the application.
    """
    deadline = opportunity.deadline
    return {
        "assembled_at": assembled_at.isoformat(),
        "organisation": {"id": str(organisation.id), "name": organisation.name},
        "opportunity": {
            "id": str(opportunity.id),
            "title": opportunity.title,
            "source_name": opportunity.source_name,
            "source_url": opportunity.source_url,
            "country": opportunity.country,
            "deadline": deadline.isoformat() if deadline else None,
            # A DATE-ONLY DEADLINE IS NOT MIDNIGHT. `deadline_is_exact` records whether a time was
            # actually stated, so nothing downstream can present an assumed midnight as a fact.
            "deadline_is_exact": bool(
                deadline and (deadline.hour or deadline.minute or deadline.second)
            ),
        },
        "application": {
            "id": str(application.id),
            "version": application.version,
            "state": application.state,
        },
        "status": status,
        "document_count": len(items),
        "documents": [item.as_manifest_entry() for item in items],
        "missing_requirements": sorted(missing),
        "needs_data": sorted(needs_data),
        "submission_mode": models.SubmissionPackage.MODE_HANDOFF,
        "note": (
            "Handoff package: prepared for a person to submit in the funder's own portal. "
            "No submission has been made."
        ),
    }


def assemble(
    db: Any,
    *,
    org_id: str,
    agent_id: str,
    application: models.Application,
    opportunity: models.Opportunity,
    organisation: models.Organisation,
    documents: list[models.Document],
    required_types: list[str],
) -> AssemblyResult:
    """Build or reuse the submission package for one application.

    Returns a result; does NOT commit. The caller owns the transaction, which is what lets the
    workflow engine record the activity and the state transition together with the package - so a
    crash between them cannot leave a package with no record of why.
    """
    from agent.document_types import canonical

    items = [
        PackageItem(
            document_id=str(doc.id),
            doc_type=doc.doc_type,
            title=doc.title,
            version=int(doc.version or 1),
            checksum_sha256=doc.checksum_sha256,
            storage_key=doc.storage_key,
            mime_type=doc.mime_type,
        )
        for doc in documents
    ]

    present = {canonical(item.doc_type) or item.doc_type for item in items}
    required = {canonical(t) or t for t in required_types}

    missing = sorted(required - present)
    # Split the gap by WHO must close it. An evidence document is the organisation's to upload; a
    # generated document that is absent means this engine or the generator has work left. Reporting
    # them as one list is how an infrastructure problem comes to look like an NGO problem.
    needs_data = sorted(set(missing) & EVIDENCE_TYPES)
    outstanding = sorted(set(missing) - EVIDENCE_TYPES)

    fingerprint, fingerprint_input = fingerprint_of(
        org_id=org_id,
        opportunity_id=str(opportunity.id),
        application_version=int(application.version or 1),
        items=items,
    )

    if missing:
        status = models.SubmissionPackage.NEEDS_DATA
        reason = "missing mandatory material: " + ", ".join(missing)
    else:
        status = models.SubmissionPackage.DRAFT
        reason = ""

    manifest = build_manifest(
        organisation=organisation,
        opportunity=opportunity,
        application=application,
        items=items,
        missing=missing,
        needs_data=needs_data,
        status=status,
        assembled_at=_now(),
    )

    # IDEMPOTENCY. The unique constraint on `idempotency_key` is the guarantee; this lookup makes the
    # common case a reuse rather than a caught IntegrityError. A package whose fingerprint has not
    # changed is the SAME package, not a new revision - a re-run must not inflate the version count.
    idempotency_key = f"package:{org_id}:{opportunity.id}:{fingerprint}"
    existing = (
        db.query(models.SubmissionPackage)
        .filter(models.SubmissionPackage.idempotency_key == idempotency_key)
        .first()
    )
    if existing is not None:
        # The manifest may be re-rendered (a deadline note, a re-ordered list) but the STATUS is only
        # advanced, never regressed, and an authorised package is left alone entirely.
        if existing.status not in models.SubmissionPackage.TERMINAL:
            existing.manifest = manifest
            existing.status_reason = reason or existing.status_reason
        return AssemblyResult(
            status=existing.status,
            fingerprint=fingerprint,
            manifest=manifest,
            included=items,
            missing=missing,
            needs_data=needs_data,
            reused=True,
            reason=existing.status_reason or reason,
        )

    package = models.SubmissionPackage(
        org_id=org_id,
        agent_id=agent_id,
        application_id=str(application.id),
        opportunity_id=str(opportunity.id),
        package_fingerprint=fingerprint,
        fingerprint_input=fingerprint_input,
        manifest=manifest,
        application_version=int(application.version or 1),
        status=status,
        status_reason=reason or None,
        submission_mode=models.SubmissionPackage.MODE_HANDOFF,
        idempotency_key=idempotency_key,
        created_at=_now(),
    )
    db.add(package)
    db.flush()

    return AssemblyResult(
        status=status,
        fingerprint=fingerprint,
        manifest=manifest,
        included=items,
        missing=missing,
        needs_data=needs_data,
        reused=False,
        reason=reason,
    )


def operator_message(result: AssemblyResult) -> str:
    """The sentence an NGO reads.

    Names the count and the blocker, because "Package failed" tells nobody what to do next and
    "Application completed" would be a lie - assembly is not submission.
    """
    if result.status == models.SubmissionPackage.NEEDS_DATA:
        if result.needs_data:
            return (
                f"Package blocked: {len(result.needs_data)} document(s) the organisation must "
                f"provide - {', '.join(result.needs_data)}."
            )
        return f"Package blocked: {', '.join(result.missing)}."
    if result.status == models.SubmissionPackage.DRAFT:
        return (
            f"Package assembled: {len(result.included)} document(s) included, ready for review. "
            "No submission has been made."
        )
    return f"Package {result.status.lower()}."
