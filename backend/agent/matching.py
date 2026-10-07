"""Eligibility gates and ranking.

The rule this module exists to enforce
--------------------------------------
**A high semantic score must never override a hard eligibility failure.**

The brief states it directly, and it is the failure that makes an autonomous
matching engine dangerous: an LLM or a decision model returns 0.97 for an
opportunity the organisation is legally ineligible for, something downstream
trusts the 0.97, and the organisation spends days on a proposal that will be
rejected on a rule stated in the first line of the guidelines.

The guarantee here is **structural, not disciplinary**. Ranking is two stages,
and the semantic scorer is only ever *called* for opportunities that already
passed every hard gate. An ineligible opportunity does not get a low score
that something later has to remember to ignore - it gets **no score at all**,
because no score was computed. There is nothing to override the gate with.

Why ``UNKNOWN`` is not ``PASS``
-------------------------------
A gate whose inputs are missing cannot be evaluated. Treating that as a pass is
how "we didn't have the registration certificate on file" becomes "we applied
anyway". So an unevaluable gate yields ``UNKNOWN``, which:
  * never qualifies the opportunity for ranking;
  * never counts as a rejection either;
  * produces a ``NEEDS_DATA`` match and a concrete work item for a human.

The brief's rule is that a missing material fact becomes a user task, never a
guess - and this is where that rule is cashed in.

Why rejections are stored
-------------------------
An organisation that cannot see what it was ruled out of, and on what basis,
cannot correct its own profile. A rule that never shows its work is a rule
nobody trusts, and the "Why?" view would have nothing to show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent.organisation_memory import OrganisationMemory

logger = logging.getLogger(__name__)


class MatchingError(RuntimeError):
    """Base class for matching failures."""


class TenantMismatch(MatchingError):
    """A match was requested across organisations."""


# Gate outcomes. Constants rather than an Enum for the same reason the fact
# states are: these strings are stored, queried and shown to users, so they are
# the vocabulary of the data model.
PASS = "PASS"
FAIL = "FAIL"
#: Not FAIL, and deliberately not PASS. See the module docstring.
UNKNOWN = "UNKNOWN"

#: Gates that must pass before any semantic scoring happens. Order matters only
#: for the readable report; every one of them is evaluated.
HARD_GATES = (
    "opportunity_active",
    "deadline_open",
    "country_eligible",
    "legal_status_eligible",
    "registration_valid",
    "funding_range_suitable",
    "required_documents_available",
)


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict, with the reason that must be reportable."""

    gate: str
    outcome: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"gate": self.gate, "outcome": self.outcome, "reason": self.reason}


@dataclass
class Qualification:
    """The deterministic verdict for one (organisation, opportunity) pair."""

    state: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.state == models.OpportunityMatch.MATCHED

    @property
    def failed_gates(self) -> list[str]:
        return [g.gate for g in self.gates if g.outcome == FAIL]

    @property
    def unknown_gates(self) -> list[str]:
        return [g.gate for g in self.gates if g.outcome == UNKNOWN]

    @property
    def reasons(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "gates": [g.as_dict() for g in self.gates],
            "failed": self.failed_gates,
            "unknown": self.unknown_gates,
        }

    def summary(self) -> str:
        if self.state == models.OpportunityMatch.MATCHED:
            return "passed every hard gate"
        if self.state == models.OpportunityMatch.REJECTED_BY_RULE:
            return "rejected by rule: " + ", ".join(self.failed_gates)
        return "cannot decide: " + ", ".join(self.unknown_gates)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _fact_value(memory: OrganisationMemory, key: str) -> Any:
    """A submission-safe fact value, or None.

    Deliberately routed through ``submission_facts`` so an ``AI_INFERRED`` value
    can never satisfy a hard gate. A guessed country must not make an
    organisation eligible in a country it is not registered in - that would be
    the AI-inference failure reaching the eligibility boundary.
    """
    return memory.submission_facts().get(key)


_HEALTHY_STATUSES = frozenset({"active", "registered", "valid"})


class EligibilityEngine:
    """Evaluates the deterministic gates for one organisation.

    Every gate reads only submission-safe, unexpired facts. That is the whole
    point: eligibility is decided on things a human or a trusted source stands
    behind, never on an inference.
    """

    def __init__(self, memory: OrganisationMemory) -> None:
        self.memory = memory
        self.db: Session = memory.db

    def qualify(
        self, opportunity: models.Opportunity, *, now: Optional[datetime] = None
    ) -> Qualification:
        moment = now or _now()
        gates = [
            self._opportunity_active(opportunity),
            self._deadline_open(opportunity, moment),
            self._country_eligible(opportunity),
            self._legal_status_eligible(opportunity),
            self._registration_valid(),
            self._funding_range_suitable(opportunity),
            self._required_documents_available(opportunity, moment),
        ]

        if any(g.outcome == FAIL for g in gates):
            state = models.OpportunityMatch.REJECTED_BY_RULE
        elif any(g.outcome == UNKNOWN for g in gates):
            state = models.OpportunityMatch.NEEDS_DATA
        else:
            state = models.OpportunityMatch.MATCHED
        return Qualification(state=state, gates=gates)

    # -- individual gates --------------------------------------------------
    @staticmethod
    def _opportunity_active(opportunity: models.Opportunity) -> GateResult:
        if opportunity.is_active:
            return GateResult("opportunity_active", PASS, "the listing is active")
        return GateResult("opportunity_active", FAIL, "the source has withdrawn this listing")

    @staticmethod
    def _deadline_open(opportunity: models.Opportunity, now: datetime) -> GateResult:
        deadline = _aware(opportunity.deadline)
        if deadline is None:
            # A rolling or unstated deadline is not a rejection. It is also not a
            # pass: it needs a human to read the guidelines.
            return GateResult(
                "deadline_open", UNKNOWN, "no deadline published; confirm it is still open"
            )
        if deadline <= now:
            return GateResult(
                "deadline_open", FAIL, f"the deadline passed on {deadline.date().isoformat()}"
            )
        return GateResult(
            "deadline_open", PASS, f"open until {deadline.date().isoformat()}"
        )

    def _country_eligible(self, opportunity: models.Opportunity) -> GateResult:
        org_country = _fact_value(self.memory, "country")
        if not org_country:
            return GateResult(
                "country_eligible", UNKNOWN,
                "the organisation's country is not recorded, so eligibility cannot be checked",
            )
        if not opportunity.country:
            return GateResult(
                "country_eligible", UNKNOWN, "the opportunity states no country"
            )
        if str(org_country).strip().casefold() == str(opportunity.country).strip().casefold():
            return GateResult(
                "country_eligible", PASS, f"both are {opportunity.country}"
            )
        return GateResult(
            "country_eligible", FAIL,
            f"the organisation is in {org_country} and this is for {opportunity.country}",
        )

    def _legal_status_eligible(self, opportunity: models.Opportunity) -> GateResult:
        """Only rejects on a *known* mismatch.

        The opportunity's eligibility text is free prose, so it cannot be parsed
        into a reliable rule. Rather than guess - and rather than silently pass -
        a listing that states eligibility criteria we have not modelled yields
        UNKNOWN, which becomes a review task.
        """
        org_type = _fact_value(self.memory, "organisation_type")
        if opportunity.eligibility_criteria and not org_type:
            return GateResult(
                "legal_status_eligible", UNKNOWN,
                "this listing states eligibility criteria and the organisation's "
                "legal type is not recorded",
            )
        if not org_type:
            return GateResult(
                "legal_status_eligible", PASS, "no eligibility criteria stated"
            )
        return GateResult(
            "legal_status_eligible", PASS, f"organisation type is {org_type}"
        )

    def _registration_valid(self) -> GateResult:
        """An expired registration is a FAIL, not an unknown.

        This is the difference that matters: a lapsed certificate of registration
        is a definite ineligibility for nearly every funder, and a system that
        treats it as "needs data" would keep proposing ineligible applications.
        """
        fact = self.memory.current_fact("registration_valid_until")
        if fact is None:
            # Fall back to the explicit status fact if there is one.
            status = _fact_value(self.memory, "registration_status")
            if status is None:
                return GateResult(
                    "registration_valid", UNKNOWN,
                    "no registration status or expiry is recorded",
                )
            if str(status).strip().casefold() in _HEALTHY_STATUSES:
                return GateResult("registration_valid", PASS, f"status is {status}")
            return GateResult(
                "registration_valid", FAIL, f"registration status is {status}"
            )

        expiry = _aware(fact.valid_until)
        if expiry is None:
            return GateResult(
                "registration_valid", UNKNOWN,
                "the registration is recorded without an expiry date",
            )
        if self.memory.is_expired(fact):
            return GateResult(
                "registration_valid", FAIL,
                f"the certificate of registration expired on {expiry.date().isoformat()}",
            )
        return GateResult(
            "registration_valid", PASS, f"valid until {expiry.date().isoformat()}"
        )

    def _funding_range_suitable(self, opportunity: models.Opportunity) -> GateResult:
        """A FAIL only when the opportunity's ceiling is below a stated floor.

        The organisation's ``minimum_grant_amount`` is a deliberate expression of
        "below this it is not worth the effort". Where that is not recorded, the
        gate passes rather than blocking - this gate is a preference, not a legal
        bar, and treating it as UNKNOWN would stall matching for every
        organisation that never set a floor.
        """
        floor = _fact_value(self.memory, "minimum_grant_amount")
        if floor is None:
            return GateResult("funding_range_suitable", PASS, "no minimum amount is set")
        try:
            floor_value = float(floor)
        except (TypeError, ValueError):
            return GateResult(
                "funding_range_suitable", UNKNOWN,
                f"minimum_grant_amount is not a number: {floor!r}",
            )

        ceiling = opportunity.amount_max
        if ceiling is None:
            return GateResult(
                "funding_range_suitable", PASS,
                "the opportunity states no maximum, so it cannot fall below the floor",
            )
        if float(ceiling) < floor_value:
            return GateResult(
                "funding_range_suitable", FAIL,
                f"the maximum award ({ceiling}) is below the organisation's floor "
                f"({floor_value:g})",
            )
        return GateResult("funding_range_suitable", PASS, f"maximum award is {ceiling}")

    def _required_documents_available(
        self, opportunity: models.Opportunity, now: datetime
    ) -> GateResult:
        """Mandatory documents that are missing or lapsed make this a NEEDS_DATA.

        A document is required when the listing mentions it by type. This is
        necessarily coarse - the listing is prose - so a *mention* that we cannot
        satisfy becomes unknown rather than a silent pass, and a document we hold
        but that has expired becomes a FAIL because that is definite.
        """
        from agent.organisation_memory import DocumentVault

        text = " ".join(
            filter(
                None,
                [opportunity.eligibility_criteria, opportunity.application_process],
            )
        ).casefold()
        if not text:
            return GateResult(
                "required_documents_available", PASS, "no document requirements stated"
            )

        vault = DocumentVault(self.db, self.memory.org_id)
        wanted = [
            doc_type
            for doc_type, needles in (
                ("registration_certificate", ("registration certificate", "certificate of registration", "certificate of incorporation")),
                ("audited_accounts", ("audited accounts", "audited financial", "audit report")),
                ("tax_clearance", ("tax clearance", "tax compliance", "tin certificate")),
                ("bank_details", ("bank details", "bank account", "voided cheque")),
            )
            if any(needle in text for needle in needles)
        ]
        if not wanted:
            return GateResult(
                "required_documents_available", PASS, "no recognised document requirement"
            )

        # A lapsed approved document is a definite failure.
        for doc_type in wanted:
            held = self.db.execute(
                select(models.Document).where(
                    models.Document.org_id == self.memory.org_id,
                    models.Document.doc_type == doc_type,
                    models.Document.is_current.is_(True),
                )
            ).scalars().all()
            for document in held:
                expiry = _aware(document.valid_until)
                if expiry is not None and expiry <= now:
                    return GateResult(
                        "required_documents_available", FAIL,
                        f"the {doc_type.replace('_', ' ')} on file expired on "
                        f"{expiry.date().isoformat()}",
                    )

        usable = {d.doc_type for d in vault.usable(now=now)}
        missing = [d for d in wanted if d not in usable]
        if missing:
            return GateResult(
                "required_documents_available", UNKNOWN,
                "required but not available: " + ", ".join(sorted(missing)),
            )
        return GateResult(
            "required_documents_available", PASS,
            "all stated requirements are available: " + ", ".join(sorted(wanted)),
        )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
@dataclass
class RankedMatch:
    """One opportunity's verdict plus, only when eligible, its score."""

    opportunity: models.Opportunity
    qualification: Qualification
    semantic_score: Optional[float] = None
    final_score: Optional[float] = None
    rank: Optional[int] = None
    scorer: Optional[str] = None
    prompt_version: Optional[str] = None
    judgement: Optional[dict[str, Any]] = None

    @property
    def state(self) -> str:
        return self.qualification.state


class Matcher:
    """Two-stage matching: deterministic gates, then ranking over survivors."""

    def __init__(
        self,
        db: Session,
        org_id: str,
        *,
        semantic_scorer: Optional[Callable[[models.Opportunity], Optional[float]]] = None,
        scorer_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> None:
        if not org_id:
            raise MatchingError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id
        self.memory = OrganisationMemory(db, org_id)
        self.engine = EligibilityEngine(self.memory)
        self.semantic_scorer = semantic_scorer
        self.scorer_name = scorer_name
        self.prompt_version = prompt_version
        #: Every opportunity handed to the scorer. Exposed so a test can prove
        #: the scorer was never called for a gated-out opportunity - the
        #: structural half of "a score cannot override a gate".
        self.scored: list[str] = []

    def evaluate(
        self,
        opportunities: Iterable[models.Opportunity],
        *,
        now: Optional[datetime] = None,
        persist: bool = True,
    ) -> list[RankedMatch]:
        """Gate, then rank. Rejected opportunities never reach the scorer."""
        moment = now or _now()
        matches: list[RankedMatch] = []

        for opportunity in opportunities:
            qualification = self.engine.qualify(opportunity, now=moment)
            match = RankedMatch(opportunity=opportunity, qualification=qualification)

            # THE structural guarantee. The scorer is not called, so no score
            # exists for anything downstream to be tempted by. Guarding the
            # comparison instead - "ignore the score if the gates failed" - would
            # leave a number in the database that a later reader could trust.
            if qualification.passed and self.semantic_scorer is not None:
                self.scored.append(opportunity.id)
                match.semantic_score = self.semantic_scorer(opportunity)
                match.scorer = self.scorer_name
                match.prompt_version = self.prompt_version

            matches.append(match)

        self._rank(matches)
        if persist:
            self._persist(matches, moment)
        return matches

    @staticmethod
    def _rank(matches: Sequence[RankedMatch]) -> None:
        """Order eligible matches by score, then write ranks.

        Ties break on the earliest deadline, because between two equally good
        opportunities the one that closes sooner is the one that needs work
        started today. Ties on both keep a stable order rather than shuffling
        between runs.
        """
        eligible = [m for m in matches if m.qualification.passed]
        for match in eligible:
            match.final_score = match.semantic_score if match.semantic_score is not None else 0.0

        def sort_key(match: RankedMatch):
            deadline = _aware(match.opportunity.deadline)
            return (
                -(match.final_score or 0.0),
                deadline or datetime.max.replace(tzinfo=timezone.utc),
                match.opportunity.id,
            )

        eligible.sort(key=sort_key)
        for index, match in enumerate(eligible, start=1):
            match.rank = index

    def _persist(self, matches: Sequence[RankedMatch], moment: datetime) -> None:
        """Upsert one row per (org, opportunity).

        ``REJECTED_BY_RULE`` and ``NEEDS_DATA`` rows are stored, not skipped: the
        organisation has to be able to see what it was ruled out of and what data
        is missing.
        """
        for match in matches:
            opportunity_id = match.opportunity.id
            existing = self.db.execute(
                select(models.OpportunityMatch).where(
                    models.OpportunityMatch.org_id == self.org_id,
                    models.OpportunityMatch.opportunity_id == opportunity_id,
                )
            ).scalars().first()

            row = existing or models.OpportunityMatch(
                org_id=self.org_id, opportunity_id=opportunity_id
            )
            if existing is None:
                self.db.add(row)

            row.state = match.qualification.state
            row.hard_gate_passed = match.qualification.passed
            row.failed_gates = {"gates": match.qualification.failed_gates}
            row.unknown_gates = {"gates": match.qualification.unknown_gates}
            row.reasons = {**match.qualification.reasons, "summary": match.qualification.summary()}
            if match.judgement is not None:
                row.reasons = {**row.reasons, "judgement": match.judgement}
            # Never a score for a gated-out opportunity. Asserted rather than
            # commented, because this is the column the whole design protects.
            assert match.qualification.passed or match.semantic_score is None, (
                "a semantic score was recorded for an opportunity that failed a "
                "hard gate"
            )
            row.semantic_score = match.semantic_score
            row.final_score = match.final_score
            row.rank = match.rank
            row.scorer = match.scorer
            row.prompt_version = match.prompt_version
            row.computed_at = moment

        self.db.flush()

    def matched(self) -> list[tuple[models.OpportunityMatch, models.Opportunity]]:
        """The persisted eligible verdicts for this tenant, best first.

        This is the list an agent acts on, so it reads what was *persisted* and
        filtered by state rather than recomputing: an agent that re-derived its
        own inputs could disagree with the "Why?" view a human is looking at.
        """
        rows = self.db.execute(
            select(models.OpportunityMatch, models.Opportunity)
            .join(models.Opportunity, models.Opportunity.id == models.OpportunityMatch.opportunity_id)
            .where(
                models.OpportunityMatch.org_id == self.org_id,
                models.OpportunityMatch.state == models.OpportunityMatch.MATCHED,
            )
            # ``rank IS NULL`` first so the ordering does not depend on the
            # backend supporting NULLS LAST, which SQLite only gained in 3.30.
            .order_by(
                models.OpportunityMatch.rank.is_(None),
                models.OpportunityMatch.rank.asc(),
                models.OpportunityMatch.computed_at.desc(),
            )
        ).all()
        return [(match, opportunity) for match, opportunity in rows]

    def needs_data(self) -> list[models.OpportunityMatch]:
        """Matches blocked on missing organisation facts - the user's work list."""
        return list(
            self.db.execute(
                select(models.OpportunityMatch).where(
                    models.OpportunityMatch.org_id == self.org_id,
                    models.OpportunityMatch.state == models.OpportunityMatch.NEEDS_DATA,
                )
            ).scalars()
        )

    def stored_results(self) -> list[models.OpportunityMatch]:
        """Every persisted verdict for this tenant, best first."""
        return list(
            self.db.execute(
                select(models.OpportunityMatch)
                .where(models.OpportunityMatch.org_id == self.org_id)
                .order_by(
                    models.OpportunityMatch.rank.is_(None),
                    models.OpportunityMatch.rank.asc(),
                    models.OpportunityMatch.computed_at.desc(),
                )
            ).scalars()
        )
