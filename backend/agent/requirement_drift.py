"""Requirement drift: a package frozen against a listing that has since changed.

THE GAP
-------
`SubmissionPackage` freezes a fingerprint, and the docstring is explicit about why:

    "A submission is not 'submit application 12': it is a specific set of documents at specific
     versions... Freeze the fingerprint, and a human authorises THAT."

That works for documents, which are frozen by checksum. It does nothing for the LISTING. A funder can
raise a budget ceiling, move a deadline, add an attachment or replace a mandatory template, and the
package keeps its fingerprint, keeps its status, and goes on reporting itself ready - because nothing
compares the requirements it was assembled against with the requirements that are true now.

So a package could be presented as READY when the thing it was built for no longer exists in that form.
The directive's §13.

WHAT DRIFT IS, AND WHAT IT IS NOT
---------------------------------
Drift is a CHANGE IN WHAT IS REQUIRED. It is not:

  * a missing document - that is readiness, and `agent/readiness.py` already answers it
  * a technical failure - that is `FAILED`
  * a deadline passing - also readiness

Keeping them apart matters because the remedies differ. Drift needs a human to decide whether the old
work still applies; a missing certificate needs an upload; a crash needs an engineer.

NOTHING IS DELETED
------------------
The directive: *"Do not overwrite immutable historical evidence. An updated opportunity must not
automatically erase valid work already completed."*

So drift is RECORDED, never applied. `detect()` reports; `mark_superseded()` is a separate, explicit
call. A package that has drifted is marked `needs_review` rather than silently rewritten, and the
existing revision is preserved so an auditor can see what was authorised at the time.
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


@dataclass
class RequirementSnapshot:
    """What a listing required at a moment. The thing drift is measured against.

    `material` holds the facts a change to which invalidates work: the deadline, the budget ceiling,
    the eligible countries, and the required document set. Each is compared individually so the
    report can say WHICH changed rather than only that something did - "the deadline moved" and "the
    required attachments changed" call for different responses from an organisation.
    """

    required_types: frozenset[str]
    deadline: Optional[datetime]
    budget_ceiling: Optional[float]
    countries: frozenset[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_types": sorted(self.required_types),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "budget_ceiling": self.budget_ceiling,
            "countries": sorted(self.countries),
        }

    def digest(self) -> str:
        """Order-independent, so a set that merely reordered is not reported as a change."""
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass
class Drift:
    """One changed fact, and whether it invalidates existing work."""

    kind: str
    detail: str
    before: Any = None
    after: Any = None
    #: True when the change means previously-correct work may no longer satisfy the funder.
    invalidates_work: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
            "invalidates_work": self.invalidates_work,
        }


@dataclass
class DriftReport:
    package_id: str
    drifted: bool
    changes: list[Drift] = field(default_factory=list)
    checked_at: Optional[datetime] = None

    @property
    def material_changes(self) -> list[Drift]:
        return [c for c in self.changes if c.invalidates_work]

    def message(self) -> str:
        """The sentence an operator reads. Names what moved, because "requirements changed" does not
        tell an organisation which of its documents to look at."""
        if not self.drifted:
            return "Package requirements are unchanged."
        names = "; ".join(c.detail for c in self.material_changes) or "; ".join(
            c.detail for c in self.changes
        )
        return (
            f"Requirements changed since this package was assembled: {names}. "
            "The package needs review before it is submitted."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "drifted": self.drifted,
            "material_change_count": len(self.material_changes),
            "changes": [c.as_dict() for c in self.changes],
            "message": self.message(),
            "checked_at": (self.checked_at or _now()).isoformat(),
        }


def snapshot_of_opportunity(opportunity: models.Opportunity) -> RequirementSnapshot:
    """What this listing requires NOW.

    `required_types` is derived from the same canonicaliser the readiness gate and the assembler use.
    A second interpretation of the requirement vocabulary is how the gate and the generator came to
    disagree once already in this project.
    """
    from agent.document_types import required_types_for_application

    text = " ".join(
        str(part or "")
        for part in (
            opportunity.title,
            getattr(opportunity, "description", None),
            getattr(opportunity, "eligibility_criteria", None),
        )
    )
    return RequirementSnapshot(
        required_types=frozenset(required_types_for_application(text)),
        deadline=getattr(opportunity, "deadline", None),
        budget_ceiling=getattr(opportunity, "budget_ceiling", None),
        countries=frozenset({opportunity.country}) if getattr(opportunity, "country", None) else frozenset(),
    )


def snapshot_from_manifest(manifest: dict[str, Any]) -> RequirementSnapshot:
    """What the package was assembled against, read from its own frozen manifest.

    Read from the manifest rather than recomputed from the listing, because the QUESTION is what the
    package was built for - recomputing would compare the present with itself and always report no
    drift.
    """
    required = set(manifest.get("required_types") or [])
    if not required:
        # Earlier manifests recorded only what was present and what was missing. Reconstruct the
        # requirement set from both, which is what the assembler actually held at the time.
        required = set(manifest.get("missing_requirements") or []) | {
            str(d.get("doc_type")) for d in (manifest.get("documents") or []) if d.get("doc_type")
        }

    deadline = None
    raw_deadline = (manifest.get("opportunity") or {}).get("deadline")
    if raw_deadline:
        try:
            deadline = datetime.fromisoformat(str(raw_deadline))
        except ValueError:
            # An unparseable stored deadline is not a drift; reporting it as one would make every
            # package look stale for a formatting reason.
            deadline = None

    return RequirementSnapshot(
        required_types=frozenset(required),
        deadline=deadline,
        budget_ceiling=manifest.get("budget_ceiling"),
        countries=frozenset(manifest.get("opportunity", {}).get("countries") or []),
    )


def detect(
    package: models.SubmissionPackage,
    opportunity: models.Opportunity,
    *,
    now: Optional[datetime] = None,
) -> DriftReport:
    """Compare what the package was frozen against with what the listing asks for now.

    Reads and reports only. Commits nothing and changes no status: a caller decides what to do, and
    the safe default is to do nothing but tell somebody.
    """
    moment = now or datetime.now(timezone.utc)
    manifest = package.manifest or {}
    was = snapshot_from_manifest(manifest)
    is_now = snapshot_of_opportunity(opportunity)

    changes: list[Drift] = []

    added = is_now.required_types - was.required_types
    removed = was.required_types - is_now.required_types
    if added:
        changes.append(Drift(
            kind="REQUIREMENTS_ADDED",
            detail=f"{len(added)} new requirement(s) the package does not contain: "
                   + ", ".join(sorted(added)),
            before=sorted(was.required_types), after=sorted(is_now.required_types),
        ))
    if removed:
        # A dropped requirement does NOT invalidate work. The package holds a document the funder no
        # longer asks for, which is untidy rather than wrong - and calling it a blocker would make an
        # organisation redo work for no reason.
        changes.append(Drift(
            kind="REQUIREMENTS_REMOVED",
            detail=f"{len(removed)} requirement(s) no longer asked for: " + ", ".join(sorted(removed)),
            before=sorted(was.required_types), after=sorted(is_now.required_types),
            invalidates_work=False,
        ))

    if was.deadline and is_now.deadline and was.deadline != is_now.deadline:
        moved_earlier = is_now.deadline < was.deadline
        changes.append(Drift(
            kind="DEADLINE_MOVED",
            detail=(
                f"the deadline moved {'earlier' if moved_earlier else 'later'} to "
                f"{is_now.deadline.date().isoformat()}"
            ),
            before=was.deadline.isoformat(), after=is_now.deadline.isoformat(),
        ))
    elif was.deadline and not is_now.deadline:
        # The listing withdrew its date. The stored one is now unsupported, which invalidates the
        # readiness judgement built on it - but the date itself is not fabricated away.
        changes.append(Drift(
            kind="DEADLINE_WITHDRAWN",
            detail="the listing no longer states a deadline; the stored one is unverified",
            before=was.deadline.isoformat(), after=None,
        ))

    if (
        was.budget_ceiling is not None
        and is_now.budget_ceiling is not None
        and was.budget_ceiling != is_now.budget_ceiling
    ):
        lower = is_now.budget_ceiling < was.budget_ceiling
        changes.append(Drift(
            kind="BUDGET_CEILING_CHANGED",
            detail=(
                f"the budget ceiling {'fell' if lower else 'rose'} to {is_now.budget_ceiling}"
            ),
            before=was.budget_ceiling, after=is_now.budget_ceiling,
        ))

    if was.countries and is_now.countries and was.countries != is_now.countries:
        changes.append(Drift(
            kind="ELIGIBLE_COUNTRIES_CHANGED",
            detail="the listing's country changed: "
                   + ", ".join(sorted(is_now.countries)),
            before=sorted(was.countries), after=sorted(is_now.countries),
        ))

    return DriftReport(
        package_id=str(package.id),
        drifted=bool(changes),
        changes=changes,
        checked_at=moment,
    )


def detect_by_id(db: Any, package_id: str) -> Optional[DriftReport]:
    """`None` for an unknown package, matching `readiness.evaluate_by_id`: a caller reporting to a
    user wants "no such package" distinct from "this package drifted"."""
    package = db.query(models.SubmissionPackage).filter(
        models.SubmissionPackage.id == package_id
    ).first()
    if package is None or not package.opportunity_id:
        return None
    opportunity = db.query(models.Opportunity).filter(
        models.Opportunity.id == package.opportunity_id
    ).first()
    if opportunity is None:
        return None
    return detect(package, opportunity)


def mark_needs_review(db: Any, package: models.SubmissionPackage, report: DriftReport) -> bool:
    """Record that a drifted package must not be presented as ready. Returns whether it changed.

    `NEEDS_DATA` rather than a new status: the model's vocabulary is fixed and adding a value would
    mean a migration and every reader learning a new word. `NEEDS_DATA` says a human must supply or
    confirm something before this moves, which is exactly the case - and `status_reason` carries the
    precise cause so nothing is lost.

    An AUTHORISED package is left alone. Its authorisation was given for a specific frozen set, and a
    later listing change must not silently revoke a decision a person made - that needs a person.
    """
    if not report.drifted or not report.material_changes:
        return False
    if package.status in models.SubmissionPackage.TERMINAL:
        return False
    if package.status == models.SubmissionPackage.AUTHORISED:
        return False
    if package.status == models.SubmissionPackage.NEEDS_DATA and package.status_reason == report.message()[:500]:
        # Already recorded, unchanged. Re-running detection must not churn the row.
        return False

    package.status = models.SubmissionPackage.NEEDS_DATA
    package.status_reason = report.message()[:500]
    return True
