"""Package readiness: the deterministic evaluation the directive calls `evaluatePackageReadiness`.

WHY IT EXISTS SEPARATELY FROM ASSEMBLY
--------------------------------------
Assembly produces a package. Readiness decides whether that package may be presented as submittable,
and it answers a different question from "did the job succeed". The directive is explicit about the
distinction and it is the one this project has broken before:

    A missing registration certificate is not an infrastructure error.
    A successful package assembly is not a successful application submission.

So readiness is evaluated ON DEMAND from persisted state rather than being frozen at assembly time.
A package assembled when the opportunity was open can become blocked because the deadline passed;
evaluating at read time means the answer is always about now.

THE FOUR OUTCOMES, AND WHY NOT FIVE
-----------------------------------
`READY`, `BLOCKED`, `ASSEMBLING`, `FAILED` - and `SUPERSEDED`, which is a property of the package row
rather than an evaluation. The directive lists five states for the package; readiness returns four and
the package status carries the fifth.

`FAILED` is reserved for a technical processing failure. A package whose documents are missing is
`BLOCKED`, never `FAILED`, because the remedy is entirely different: one needs an engineer, the other
needs the organisation to upload a certificate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import models

#: The readiness verdicts. Distinct from `SubmissionPackage`'s status column, which records what has
#: happened to the package rather than whether it may be sent.
READY = "READY"
BLOCKED = "BLOCKED"
ASSEMBLING = "ASSEMBLING"
FAILED = "FAILED"


@dataclass
class ReadinessBlocker:
    """One reason the package may not be submitted, in the form an operator can act on."""

    code: str
    detail: str
    #: True when the ORGANISATION must act, False when the platform or an engineer must. This drives
    #: the operator message and prevents an NGO gap being reported as an outage.
    needs_organisation: bool = False


@dataclass
class Readiness:
    """The verdict, with everything needed to explain it."""

    package_id: str
    verdict: str
    blockers: list[ReadinessBlocker] = field(default_factory=list)
    satisfied: int = 0
    required: int = 0
    evaluated_at: Optional[datetime] = None

    @property
    def is_ready(self) -> bool:
        return self.verdict == READY

    @property
    def organisation_actions(self) -> list[str]:
        return [b.detail for b in self.blockers if b.needs_organisation]

    @property
    def platform_actions(self) -> list[str]:
        return [b.detail for b in self.blockers if not b.needs_organisation]

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "verdict": self.verdict,
            "satisfied": self.satisfied,
            "required": self.required,
            "blockers": [
                {"code": b.code, "detail": b.detail, "needs_organisation": b.needs_organisation}
                for b in self.blockers
            ],
            "evaluated_at": (self.evaluated_at or datetime.now(timezone.utc)).isoformat(),
        }

    def message(self) -> str:
        """The sentence an NGO reads.

        Names the count and the blocker. "Package failed" tells nobody what to do next, and the
        directive quotes exactly that as the bad example.
        """
        if self.verdict == READY:
            return (
                f"Package ready: {self.satisfied} of {self.required} mandatory requirements "
                "satisfied. Not yet submitted - a person authorises and submits this."
            )
        if self.verdict == BLOCKED:
            if self.organisation_actions:
                return f"Package blocked: {self.organisation_actions[0]}"
            if self.platform_actions:
                return f"Package blocked: {self.platform_actions[0]}"
            return "Package blocked."
        if self.verdict == ASSEMBLING:
            return "Package is still being assembled."
        return "Package could not be validated."


def evaluate(package: models.SubmissionPackage, *, now: Optional[datetime] = None) -> Readiness:
    """Evaluate one package against everything that must hold before it may be submitted.

    Reads PERSISTED STATE and the opportunity's current facts, so the answer reflects now rather than
    the moment of assembly.
    """
    moment = now or datetime.now(timezone.utc)
    manifest = package.manifest or {}
    blockers: list[ReadinessBlocker] = []

    # --- a technical failure is its own verdict ------------------------------------------------
    if package.status == models.SubmissionPackage.FAILED_FINAL:
        return Readiness(
            package_id=str(package.id), verdict=FAILED,
            blockers=[ReadinessBlocker("TECHNICAL_FAILURE", package.status_reason or "assembly failed")],
            evaluated_at=moment,
        )

    if package.status == models.SubmissionPackage.SUPERSEDED:
        return Readiness(
            package_id=str(package.id), verdict=BLOCKED,
            blockers=[ReadinessBlocker(
                "SUPERSEDED",
                "a newer package revision replaced this one; the authorisation applied to the "
                "previous set of documents",
            )],
            evaluated_at=moment,
        )

    if package.status == models.SubmissionPackage.SUBMITTING:
        return Readiness(
            package_id=str(package.id), verdict=ASSEMBLING,
            blockers=[ReadinessBlocker("SUBMISSION_IN_FLIGHT", "a submission is already in progress")],
            evaluated_at=moment,
        )

    # --- required material ---------------------------------------------------------------------
    missing = list(manifest.get("missing_requirements") or [])
    needs_data = list(manifest.get("needs_data") or [])
    documents = manifest.get("documents") or []

    if missing:
        for name in missing:
            is_org_gap = name in needs_data
            blockers.append(ReadinessBlocker(
                "MISSING_DOCUMENT",
                # The wording differs because the remedy differs. One is an upload, the other is the
                # platform having work left to do - and conflating them is how an NGO is asked to
                # provide something the system was supposed to produce.
                (f"verified {name.replace('_', ' ')} missing"
                 if is_org_gap else f"{name.replace('_', ' ')} has not been prepared"),
                needs_organisation=is_org_gap,
            ))

    # --- the opportunity must still be open ----------------------------------------------------
    opportunity = getattr(package, "_opportunity", None)
    if opportunity is None:
        blockers.append(ReadinessBlocker(
            "OPPORTUNITY_UNKNOWN", "the opportunity could not be loaded for this package"
        ))
    else:
        deadline_exact = manifest.get("opportunity", {}).get("deadline_is_exact")
        deadline = opportunity.deadline
        if deadline is not None:
            aware = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
            if aware <= moment:
                blockers.append(ReadinessBlocker(
                    "OPPORTUNITY_CLOSED",
                    f"the deadline passed on {aware.date().isoformat()}; a complete package for a "
                    "closed opportunity is not submittable",
                ))
            elif not deadline_exact:
                # NOT a blocker. It IS a warning the operator needs, because a date-only deadline is
                # not midnight and treating it as one would cut off a valid application.
                blockers.append(ReadinessBlocker(
                    "DEADLINE_DATE_ONLY",
                    "the funder published a date without a time; confirm the closing time before "
                    "submitting",
                ))
        if not opportunity.is_active:
            blockers.append(ReadinessBlocker(
                "OPPORTUNITY_INACTIVE", "the opportunity is no longer active in the catalogue"
            ))

    # --- nothing may be ready with no material at all ------------------------------------------
    if not documents:
        blockers.append(ReadinessBlocker(
            "NO_DOCUMENTS",
            "the package contains no documents",
            needs_organisation=bool(needs_data),
        ))

    # A DATE-ONLY DEADLINE warns without blocking; everything else blocks.
    blocking = [b for b in blockers if b.code != "DEADLINE_DATE_ONLY"]
    if blocking:
        verdict = BLOCKED
    elif not package.status or package.status == models.SubmissionPackage.NEEDS_DATA:
        verdict = ASSEMBLING
    else:
        verdict = READY

    required = len(missing) + len(documents) + (1 if not documents and not missing else 0)
    return Readiness(
        package_id=str(package.id),
        verdict=verdict,
        # Warnings are returned too, so an operator sees the deadline caveat even on a ready package.
        blockers=blockers,
        satisfied=len(documents),
        required=required,
        evaluated_at=moment,
    )


def evaluate_by_id(db: Any, package_id: str, *, now: Optional[datetime] = None) -> Optional[Readiness]:
    """The function the directive names, in Granada's conventions.

    Returns None when the package does not exist rather than raising: a caller reporting to a user
    wants to distinguish "no such package" from "this package is blocked", and an exception forces
    both into the same shape.
    """
    package = db.query(models.SubmissionPackage).filter(
        models.SubmissionPackage.id == package_id
    ).first()
    if package is None:
        return None
    if package.opportunity_id:
        # Attached rather than loaded into a column: readiness needs the opportunity but must not
        # mutate the package row to get it, because evaluating is a READ.
        package._opportunity = db.query(models.Opportunity).filter(
            models.Opportunity.id == package.opportunity_id
        ).first()
    return evaluate(package, now=now)
