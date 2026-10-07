"""The Donor Research specialist.

What it does, and what it refuses to do
---------------------------------------
It builds a structured, **provenance-stamped** research record for one
opportunity, out of what Granada already knows: the opportunity catalogue row, the
raw producer payload, the organisation's own submission-safe facts, and the
eligibility result.

It does **not** invent donor information. The brief is explicit, and the mechanism
is the same one that guards organisation facts: every field carries its epistemic
class, and ``UNKNOWN`` is a first-class value rather than an omission. A missing
field reads as "nothing to say"; ``UNKNOWN`` reads as "somebody must find this
out", which is the difference between an incomplete record and a misleading one.

The four classes
----------------
``SOURCE_FACT``
    Quoted from the opportunity record, traceable through ``source_references``.
``DERIVED_OBSERVATION``
    Computed from source facts - days to deadline, a funding-range overlap. True
    by construction.
``AI_INFERENCE``
    A model's reading of prose. Never treated as a donor fact, exactly as an
    ``AI_INFERRED`` organisation fact is never submission-safe.
``UNKNOWN``
    Not established. Recorded, not omitted.

No network access happens here. The phase deliberately researches from the
catalogue rather than crawling, because crawling is a much larger capability with
its own compliance surface - and the brief says not to let enthusiasm pull later
phases forward. When external retrieval arrives it fills the same structure with
richer ``SOURCE_FACT`` values and stores the source metadata to reproduce what was
used.

Versioning
----------
Research is **appended, never overwritten**. If an opportunity changes materially,
a new version is created; an application keeps the version it was built against.
An application that cannot say which research it used cannot be explained later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

#: Bump when the *meaning* of a field changes. Recorded per row so a consumer can
#: tell which contract produced it.
RESEARCH_VERSION = "v1"

SOURCE_FACT = models.DonorResearch.SOURCE_FACT
DERIVED_OBSERVATION = models.DonorResearch.DERIVED_OBSERVATION
AI_INFERENCE = models.DonorResearch.AI_INFERENCE
UNKNOWN = models.DonorResearch.UNKNOWN

#: Phrases that indicate a document requirement, mapped to a vault doc_type. The
#: same coarse matching the eligibility gate uses, kept in one place so the two
#: cannot drift into disagreeing about what a listing requires.
_DOCUMENT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("registration_certificate", ("registration certificate", "certificate of registration", "certificate of incorporation")),
    ("audited_accounts", ("audited accounts", "audited financial", "audit report")),
    ("tax_clearance", ("tax clearance", "tax compliance", "tin certificate")),
    ("bank_details", ("bank details", "bank account", "voided cheque")),
    ("budget_narrative", ("budget narrative", "detailed budget", "line-item budget")),
    ("logframe", ("logframe", "logical framework", "results framework")),
    ("safeguarding_policy", ("safeguarding policy", "child protection policy")),
)

#: Phrases that indicate what the funder wants written. Kept separate from
#: documents because they are answered, not attached.
_SECTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("needs_statement", ("statement of need", "problem statement", "needs assessment")),
    ("approach", ("methodology", "approach", "implementation strategy")),
    ("outcomes", ("outcomes", "expected results", "indicators")),
    ("sustainability", ("sustainability", "exit strategy")),
    ("budget_justification", ("budget justification", "value for money")),
    ("organisational_capacity", ("organisational capacity", "track record", "past performance")),
    ("risk", ("risk assessment", "risk management", "mitigation")),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass
class ResearchResult:
    """The structured outcome, before it is persisted."""

    donor_identity: dict[str, Any] = field(default_factory=dict)
    programme_priorities: dict[str, Any] = field(default_factory=dict)
    eligibility_observations: dict[str, Any] = field(default_factory=dict)
    application_instructions: Optional[str] = None
    funding_range: dict[str, Any] = field(default_factory=dict)
    deadline: Optional[datetime] = None
    required_documents: dict[str, Any] = field(default_factory=dict)
    required_sections: dict[str, Any] = field(default_factory=dict)
    submission_mechanism: Optional[str] = None
    contacts: dict[str, Any] = field(default_factory=dict)
    risks: dict[str, Any] = field(default_factory=dict)
    unknowns: dict[str, Any] = field(default_factory=dict)
    #: field name -> epistemic class. Every populated field appears here, so a
    #: consumer cannot use a value without having been told what kind it is.
    fact_classes: dict[str, str] = field(default_factory=dict)
    source_references: dict[str, Any] = field(default_factory=dict)
    opportunity_version: Optional[int] = None

    def classify(self, field_name: str, value: Any, klass: str) -> None:
        """Record a field and its class together, so they cannot separate."""
        if value is None or value == {} or value == []:
            self.unknowns[field_name] = "not established from available sources"
            self.fact_classes[field_name] = UNKNOWN
            return
        self.fact_classes[field_name] = klass

    @property
    def inferred_fields(self) -> list[str]:
        return sorted(k for k, v in self.fact_classes.items() if v == AI_INFERENCE)

    @property
    def unknown_fields(self) -> list[str]:
        return sorted(self.unknowns)


class DonorResearchService:
    """Researches one opportunity on behalf of one agent."""

    def __init__(self, db: Session, agent: models.GranadaAgent) -> None:
        self.db = db
        self.agent = agent

    # ------------------------------------------------------------------
    def research(
        self,
        opportunity: models.Opportunity,
        *,
        application_id: Optional[str] = None,
        eligibility: Optional[dict[str, Any]] = None,
    ) -> models.DonorResearch:
        """Produce and persist research, idempotent per opportunity revision.

        **Crash recovery is why this is idempotent.** A worker that persists
        research and then dies before advancing the workflow will have its job
        recovered and re-executed. If research always appended, that recovery
        would leave *two* versions for one opportunity revision - and downstream a
        proposal could not tell which one it was built on.

        So: if a version already exists for this opportunity revision and this
        agent, it is returned rather than duplicated. A genuinely *new* revision
        still appends, because that is the change-detection case rather than the
        retry case.
        """
        existing = self.current(opportunity.id)
        current_version = getattr(opportunity, "version", 1) or 1
        if existing is not None and (existing.opportunity_version or 0) == current_version:
            return existing

        result = self.build(opportunity, eligibility=eligibility)
        return self.persist(result, opportunity, application_id=application_id)

    # ------------------------------------------------------------------
    def build(
        self, opportunity: models.Opportunity, *, eligibility: Optional[dict[str, Any]] = None
    ) -> ResearchResult:
        """Assemble the result from what Granada actually knows.

        Every branch either quotes the source, computes from it, or records
        UNKNOWN. There is no branch that guesses and calls it a fact.
        """
        result = ResearchResult()
        opportunity_version = getattr(opportunity, "version", None)
        result.opportunity_version = opportunity_version

        # -- donor identity: quoted ---------------------------------------
        donor = {
            "source_name": opportunity.source_name,
            "source_url": opportunity.source_url,
            "country": opportunity.country,
        }
        result.donor_identity = donor
        result.classify("donor_identity", donor, SOURCE_FACT)

        # -- funding range: quoted, with a derived note --------------------
        funding = {
            "currency": opportunity.currency,
            "amount_min": opportunity.amount_min,
            "amount_max": opportunity.amount_max,
        }
        result.funding_range = {k: v for k, v in funding.items() if v is not None}
        result.classify("funding_range", result.funding_range, SOURCE_FACT)

        # -- deadline: quoted, plus an arithmetic observation --------------
        deadline = _aware(opportunity.deadline)
        result.deadline = deadline
        if deadline is not None:
            days = (deadline - _now()).days
            result.classify("deadline", deadline, SOURCE_FACT)
            result.eligibility_observations["days_remaining"] = days
            result.fact_classes["days_remaining"] = DERIVED_OBSERVATION
            if days < 0:
                result.risks["deadline"] = "the deadline has already passed"
        else:
            result.classify("deadline", None, UNKNOWN)

        # -- what the listing says it wants -------------------------------
        prose = " ".join(
            filter(
                None,
                [
                    opportunity.eligibility_criteria,
                    opportunity.application_process,
                    opportunity.description,
                ],
            )
        ).casefold()

        documents = {
            doc_type: True
            for doc_type, needles in _DOCUMENT_HINTS
            if any(needle in prose for needle in needles)
        }
        result.required_documents = documents
        result.classify("required_documents", documents, SOURCE_FACT)

        sections = {
            section: True
            for section, needles in _SECTION_HINTS
            if any(needle in prose for needle in needles)
        }
        result.required_sections = sections
        result.classify("required_sections", sections, SOURCE_FACT)

        result.application_instructions = opportunity.application_process
        result.classify(
            "application_instructions", opportunity.application_process, SOURCE_FACT
        )

        # -- eligibility observations: passed in, not re-derived -----------
        if eligibility:
            result.eligibility_observations.update(
                {
                    "hard_gate_passed": eligibility.get("hard_gate_passed"),
                    "failed_gates": eligibility.get("failed_gates", []),
                    "unknown_gates": eligibility.get("unknown_gates", []),
                }
            )
            result.fact_classes["eligibility_observations"] = DERIVED_OBSERVATION

        # -- contacts: quoted ---------------------------------------------
        contacts = {
            k: v
            for k, v in {
                "email": opportunity.contact_email,
                "phone": opportunity.contact_phone,
            }.items()
            if v
        }
        result.contacts = contacts
        result.classify("contacts", contacts, SOURCE_FACT)

        # -- submission mechanism: derived from the prose, or unknown ------
        mechanism = None
        if opportunity.application_process or opportunity.eligibility_criteria:
            if any(word in prose for word in ("online portal", "apply online", "submit online")):
                mechanism = "ONLINE_PORTAL"
            elif any(word in prose for word in ("email", "send to", "submit by email")):
                mechanism = "EMAIL"
            elif any(word in prose for word in ("post", "courier", "hard copy")):
                mechanism = "POST"
        result.submission_mechanism = mechanism
        result.classify("submission_mechanism", mechanism, DERIVED_OBSERVATION)

        # -- programme priorities: quoted from the structured tags ---------
        priorities = {
            k: v
            for k, v in {
                "keywords": opportunity.keywords,
                "focus_areas": opportunity.focus_areas,
                "sector": opportunity.sector,
            }.items()
            if v
        }
        result.programme_priorities = priorities
        result.classify("programme_priorities", priorities, SOURCE_FACT)

        # -- risks: arithmetic and structural only, never speculation ------
        if opportunity.amount_max is None and opportunity.amount_min is None:
            result.risks["funding_range"] = "the listing states no funding range"
        if not opportunity.eligibility_criteria:
            result.risks["eligibility"] = (
                "the listing states no eligibility criteria, so eligibility cannot "
                "be confirmed from the catalogue alone"
            )
        if not opportunity.application_process:
            result.risks["application_process"] = (
                "the listing states no application process"
            )

        # -- provenance ----------------------------------------------------
        result.source_references = {
            "opportunity_id": opportunity.id,
            "opportunity_version": opportunity_version,
            "opportunity_content_hash": opportunity.content_hash,
            "source_url": opportunity.source_url,
            "source_name": opportunity.source_name,
            "contract_version": getattr(opportunity, "contract_version", None),
            "researched_from": "opportunity_catalogue",
            "researcher_version": RESEARCH_VERSION,
        }
        return result

    # ------------------------------------------------------------------
    def persist(
        self,
        result: ResearchResult,
        opportunity: models.Opportunity,
        *,
        application_id: Optional[str] = None,
    ) -> models.DonorResearch:
        """Append a version and mark the previous one superseded."""
        previous = self.current(opportunity.id)
        version = (previous.version + 1) if previous else 1
        if previous is not None:
            previous.is_current = False

        row = models.DonorResearch(
            agent_id=self.agent.id,
            org_id=self.agent.org_id,
            opportunity_id=opportunity.id,
            application_id=application_id,
            version=version,
            is_current=True,
            donor_identity=result.donor_identity or None,
            programme_priorities=result.programme_priorities or None,
            eligibility_observations=result.eligibility_observations or None,
            application_instructions=result.application_instructions,
            funding_range=result.funding_range or None,
            deadline=result.deadline,
            required_documents=result.required_documents or None,
            required_sections=result.required_sections or None,
            submission_mechanism=result.submission_mechanism,
            contacts=result.contacts or None,
            risks=result.risks or None,
            unknowns=result.unknowns or None,
            fact_classes=result.fact_classes or None,
            source_references=result.source_references or None,
            opportunity_version=result.opportunity_version,
            research_version=RESEARCH_VERSION,
            researched_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def current(self, opportunity_id: str) -> Optional[models.DonorResearch]:
        return self.db.execute(
            select(models.DonorResearch)
            .where(
                models.DonorResearch.opportunity_id == opportunity_id,
                models.DonorResearch.agent_id == self.agent.id,
                models.DonorResearch.is_current.is_(True),
            )
            .order_by(models.DonorResearch.version.desc())
        ).scalars().first()

    def history(self, opportunity_id: str) -> list[models.DonorResearch]:
        return list(
            self.db.execute(
                select(models.DonorResearch)
                .where(
                    models.DonorResearch.opportunity_id == opportunity_id,
                    models.DonorResearch.agent_id == self.agent.id,
                )
                .order_by(models.DonorResearch.version.asc())
            ).scalars()
        )

    def is_stale(self, opportunity: models.Opportunity) -> bool:
        """Whether the current research predates the opportunity's revision.

        This is how "the listing changed materially" becomes "research again"
        rather than "use the stale answer", and it is the reason research records
        ``opportunity_version`` at all.
        """
        current = self.current(opportunity.id)
        if current is None:
            return True
        return (current.opportunity_version or 0) < (getattr(opportunity, "version", 1) or 1)

    def usable(self, opportunity_id: str, *, allow_inference: bool = False) -> dict[str, Any]:
        """The researched fields a downstream specialist may rely on.

        ``AI_INFERENCE`` values are excluded by default, for the same reason
        ``AI_INFERRED`` organisation facts are excluded from a submission: an
        inference must not become a fact by being read.
        """
        current = self.current(opportunity_id)
        if current is None:
            return {}
        classes = current.fact_classes or {}
        out: dict[str, Any] = {}
        for field_name in (
            "donor_identity", "programme_priorities", "eligibility_observations",
            "application_instructions", "funding_range", "deadline",
            "required_documents", "required_sections", "submission_mechanism",
            "contacts", "risks",
        ):
            klass = classes.get(field_name, UNKNOWN)
            if klass == AI_INFERENCE and not allow_inference:
                continue
            if klass == UNKNOWN:
                continue
            value = getattr(current, field_name, None)
            if value is not None:
                out[field_name] = value
        return out
