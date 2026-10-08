"""Granada's own decision engine. Standalone: no vendor, no API key, no network.

WHY THIS REPLACES JEV
---------------------
The provider it replaces needed an account with an external service and an API key to answer
questions about an organisation's own data. That is the wrong shape for this product: it makes a
self-hosted deployment depend on a vendor being reachable, and it puts an NGO's eligibility data
through a third party to answer a question Granada could answer itself.

**The brief never asked for it.** It asks for a *provider-neutral model gateway* and says "Do not
couple business workflows directly to one LLM vendor". A vendor is one way to satisfy that; a
self-contained engine is a better one, because it cannot go down, cannot be rate-limited, and costs
nothing per decision.

WHAT IT IS
----------
A transparent weighted-evidence engine. Every answer is produced by:

1. **Declared signals**, derived from the state Granada already holds - the organisation's profile,
   the opportunity's attributes, the application's progress. Each signal says what it is, where it
   came from, and how much it should move the answer.
2. **An explicit aggregation**, per question type, that anyone can read and check.
3. **A computed confidence**, from evidence COVERAGE and the MARGIN between the leading answers -
   never a constant, and never assumed from the fact that an answer parsed.

THE TWO RULES THAT MATTER
-------------------------
* **It never guesses.** A question with no evidence bearing on it is left unanswered and the
  provider raises `DecisionProviderUnavailable`. The gateway keys its fallback on that, so a guess
  would silently remove Granada's ability to tell "answered" from "failed".
* **It never fabricates confidence.** A missing confidence is reported as missing, which the
  gateway treats as the LOW band. An unearned HIGH would let this engine qualify for autonomy it
  has not demonstrated - the same trap the previous provider's docstring warned about.

THE "WHY?" VIEW
---------------
Every signal is carried onto the recorded decision, so the brief's requirement that "every
automated decision should have a 'Why?' view with evidence" is satisfied by the same data that
produced the answer rather than by a separate explanation written afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from agent.decision.models import Answer, DecisionRequest, DecisionResult, QuestionType
from agent.decision.providers.base import BaseDecisionProvider

#: How much total evidence is "enough" to answer with full coverage confidence.
ENOUGH_EVIDENCE = 3.0

#: A BOOLEAN needs net support at or above this to answer True. Above zero rather than at an
#: arbitrary midpoint, because the signals are already directional: a signal only exists when
#: something in the data spoke to the question.
BOOLEAN_THRESHOLD = 0.0


@dataclass(frozen=True)
class Signal:
    """One piece of evidence bearing on one question.

    Deliberately concrete. A signal that cannot say where it came from is not evidence, it is an
    opinion, and the "Why?" view would have nothing to show.
    """

    key: str
    value: Any
    weight: float
    source: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "weight": round(self.weight, 4),
            "source": self.source,
            "detail": self.detail,
        }


# ===========================================================================
# Declared signals, derived from Granada's own data
# ===========================================================================
def _get(state: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    """Read a nested key, tolerating absent branches.

    Missing data must produce NO signal rather than a default one. A defaulted signal is a
    confident-sounding answer built on nothing.
    """
    current: Any = state
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _boolean_signal(
    key: str, condition: Any, *, weight: float, source: str, when_true: str, when_false: str
) -> Optional[Signal]:
    if condition is None:
        return None
    return Signal(
        key=key,
        value=bool(condition),
        weight=weight,
        source=source,
        detail=when_true if condition else when_false,
    )


def opportunity_triage_signals(state: Mapping[str, Any]) -> list[Signal]:
    """Is this opportunity worth the organisation's time?

    The first gate is ELIGIBILITY, because it is deterministic and a wrong answer wastes real
    effort. Fit is secondary and weighted lower, because it is a judgement.
    """
    signals: list[Signal] = []

    eligible = _get(state, "opportunity", "eligible")
    signal = _boolean_signal(
        "worth_pursuing",
        eligible,
        weight=3.0,
        source="opportunity.eligible",
        when_true="the organisation meets the funder's stated eligibility criteria",
        when_false="the organisation does not meet the stated eligibility criteria",
    )
    if signal:
        signals.append(signal)

    deadline_days = _get(state, "opportunity", "days_to_deadline")
    if isinstance(deadline_days, (int, float)):
        signals.append(
            Signal(
                key="worth_pursuing",
                value=deadline_days >= 0,
                weight=1.0 if deadline_days >= 14 else 0.5,
                source="opportunity.days_to_deadline",
                detail=(
                    f"{int(deadline_days)} days remain"
                    if deadline_days >= 14
                    else f"only {int(deadline_days)} days remain"
                    if deadline_days >= 0
                    else "the deadline has passed"
                ),
            )
        )

    fit = _get(state, "match", "fit_score")
    if isinstance(fit, (int, float)):
        signals.append(
            Signal(
                key="worth_pursuing",
                value=fit >= 0.5,
                weight=min(1.0, max(0.0, float(fit))),
                source="match.fit_score",
                detail=f"the stored match fit is {float(fit):.2f}",
            )
        )

    return signals


def email_triage_signals(state: Mapping[str, Any]) -> list[Signal]:
    """Does this inbound message need a person, and how soon?"""
    signals: list[Signal] = []

    classification = _get(state, "message", "classification")
    if isinstance(classification, str) and classification:
        # A closed classification, so the mapping is a decision rather than a heuristic.
        urgent = classification in {
            "DEADLINE_CHANGE",
            "REJECTION_NOTICE",
            "AWARD_NOTICE",
            "INTERVIEW_INVITATION",
            "DOCUMENT_REQUEST",
        }
        signals.append(
            Signal(
                key="needs_human",
                value=urgent,
                weight=2.0 if urgent else 1.0,
                source="message.classification",
                detail=f"classified as {classification}",
            )
        )

    sender_known = _get(state, "message", "sender_known")
    signal = _boolean_signal(
        "needs_human",
        sender_known,
        weight=0.5,
        source="message.sender_known",
        when_true="the sender is a known contact",
        when_false="the sender is not known to this organisation",
    )
    if signal:
        signals.append(signal)

    scan = _get(state, "message", "scan_verdict")
    if isinstance(scan, str) and scan and scan != "CLEAN":
        signals.append(
            Signal(
                key="needs_human",
                value=True,
                weight=3.0,
                source="message.scan_verdict",
                detail=f"an attachment scan reported {scan}",
            )
        )

    return signals


def application_readiness_signals(state: Mapping[str, Any]) -> list[Signal]:
    """Is this application ready to go, and is anything blocking it?"""
    signals: list[Signal] = []

    blocked = _get(state, "application", "blocking_conditions")
    if isinstance(blocked, (list, tuple)):
        signals.append(
            Signal(
                key="ready_to_submit",
                value=len(blocked) == 0,
                weight=3.0 if blocked else 2.0,
                source="application.blocking_conditions",
                detail=(
                    f"{len(blocked)} condition(s) block submission"
                    if blocked
                    else "no condition blocks submission"
                ),
            )
        )

    missing = _get(state, "application", "missing_documents")
    if isinstance(missing, (list, tuple)):
        signals.append(
            Signal(
                key="ready_to_submit",
                value=len(missing) == 0,
                weight=2.0 if missing else 1.0,
                source="application.missing_documents",
                detail=(
                    f"{len(missing)} required document(s) are outstanding"
                    if missing
                    else "every required document is held"
                ),
            )
        )

    approved = _get(state, "application", "human_approved")
    signal = _boolean_signal(
        "ready_to_submit",
        approved,
        weight=1.5,
        source="application.human_approved",
        when_true="a person has authorised this exact package",
        when_false="no person has authorised this package",
    )
    if signal:
        signals.append(signal)

    return signals


def agent_routing_signals(state: Mapping[str, Any]) -> list[Signal]:
    """Which specialist should handle this, as a CHOICE over the allowed routes."""
    route = _get(state, "event", "suggested_route")
    if not isinstance(route, str) or not route:
        return []
    return [
        Signal(
            key="route",
            value=route,
            weight=1.0,
            source="event.suggested_route",
            detail=f"the event carries the route {route}",
        )
    ]


#: Which extractors run for which decision type. A decision type with no entry produces NO
#: signals, so the provider declines rather than answering from nothing.
SIGNAL_EXTRACTORS: dict[str, Any] = {
    "opportunity_triage": opportunity_triage_signals,
    "email_triage": email_triage_signals,
    "application_readiness": application_readiness_signals,
    "agent_routing": agent_routing_signals,
}


def derive_signals(decision_type: str, state: Mapping[str, Any]) -> list[Signal]:
    """Every signal bearing on a decision, from the declared extractors plus any supplied.

    A caller may pass `state["signals"]` directly - a list of mappings with the same fields. That
    is how a future specialist signals something the generic extractors do not know about, without
    this file having to anticipate it.
    """
    signals: list[Signal] = []

    extractor = SIGNAL_EXTRACTORS.get(decision_type)
    if extractor is not None:
        signals.extend(extractor(state))

    supplied = state.get("signals")
    if isinstance(supplied, (list, tuple)):
        for entry in supplied:
            if not isinstance(entry, Mapping):
                continue
            try:
                signals.append(
                    Signal(
                        key=str(entry["key"]),
                        value=entry.get("value"),
                        weight=float(entry.get("weight", 1.0)),
                        source=str(entry.get("source", "state.signals")),
                        detail=str(entry.get("detail", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

    return signals


# ===========================================================================
# The provider
# ===========================================================================
class LocalDecisionProvider(BaseDecisionProvider):
    """Granada's own decision engine.

    Always available: it has no dependency that can be absent.
    """

    name = "local"
    available = True

    def __init__(self, *, signals: Optional[Iterable[Signal]] = None) -> None:
        #: Optional extra signals, for tests and for callers that know something the extractors
        #: cannot see. Kept as a constructor argument rather than a module global so two
        #: providers in one process cannot contaminate each other.
        self._extra: tuple[Signal, ...] = tuple(signals or ())

    # -- aggregation ----------------------------------------------------
    @staticmethod
    def _aggregate_boolean(question, signals: Sequence[Signal]) -> tuple[Any, float]:
        support = 0.0
        total = 0.0
        for signal in signals:
            total += signal.weight
            support += signal.weight if signal.value else -signal.weight
        answered = support >= BOOLEAN_THRESHOLD
        # The margin is how decisively the evidence pointed, NOT how much of it there was: three
        # weak signals that disagree are not a confident answer.
        margin = abs(support) / total if total else 0.0
        return answered, margin

    @staticmethod
    def _aggregate_choice(question, signals: Sequence[Signal]) -> tuple[Any, float]:
        tally: dict[str, float] = {}
        for signal in signals:
            if signal.value in question.options:
                tally[str(signal.value)] = tally.get(str(signal.value), 0.0) + signal.weight
        if not tally:
            raise ValueError("no signal named an option this question allows")
        ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
        leader, lead_weight = ranked[0]
        total = sum(tally.values())
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = (lead_weight - runner_up) / total if total else 0.0
        return leader, margin

    @staticmethod
    def _aggregate_score(question, signals: Sequence[Signal]) -> tuple[Any, float]:
        net = 0.0
        total = 0.0
        for signal in signals:
            total += signal.weight
            net += signal.weight if signal.value else -signal.weight
        span = (question.maximum or 0) - (question.minimum or 0)
        if span <= 0:
            raise ValueError("a SCORE question needs a positive range")
        # Map [-1, 1] onto the question's declared range.
        position = (net / total) if total else 0.0
        value = int(round((question.minimum or 0) + (position + 1) / 2 * span))
        value = max(question.minimum or 0, min(question.maximum or 0, value))
        margin = abs(position)
        return value, margin

    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        signals = list(self._extra) + derive_signals(request.decision_type, request.state)

        by_key: dict[str, list[Signal]] = {}
        for signal in signals:
            by_key.setdefault(signal.key, []).append(signal)

        answers: dict[str, Answer] = {}
        unanswered: list[str] = []
        margins: list[float] = []
        coverage_weights: list[float] = []

        for question in request.questions:
            bearing = by_key.get(question.key, [])
            if not bearing:
                # NO EVIDENCE IS NOT AN ANSWER. The gateway keys its fallback on this, so
                # guessing here would silently remove "answered" vs "failed".
                unanswered.append(question.key)
                continue

            try:
                if question.type == QuestionType.BOOLEAN:
                    value, margin = self._aggregate_boolean(question, bearing)
                elif question.type == QuestionType.CHOICE:
                    value, margin = self._aggregate_choice(question, bearing)
                elif question.type == QuestionType.SCORE:
                    value, margin = self._aggregate_score(question, bearing)
                else:  # pragma: no cover - DecisionQuestion validates the type on construction
                    unanswered.append(question.key)
                    continue
            except ValueError:
                unanswered.append(question.key)
                continue

            total_weight = sum(s.weight for s in bearing)
            coverage = min(1.0, total_weight / ENOUGH_EVIDENCE)
            # Confidence is COVERAGE x MARGIN. Both must be real: plenty of evidence that
            # disagrees is not confidence, and a decisive margin from one weak signal is not
            # either. This is the whole reason the previous provider reported confidence=None
            # rather than 1.0 when the vendor did not supply one.
            confidence = round(coverage * margin, 4)
            margins.append(margin)
            coverage_weights.append(coverage)

            answers[question.key] = Answer(
                key=question.key, value=value, confidence=confidence
            )

        if not answers:
            from agent.decision.exceptions import DecisionProviderUnavailable

            raise DecisionProviderUnavailable(
                f"no evidence bears on any of {len(request.questions)} question(s) for "
                f"{request.decision_type!r}; this engine does not guess"
            )

        evidence = [s.as_dict() for s in signals]
        overall = (
            round(
                sum(margins) / len(margins) * sum(coverage_weights) / len(coverage_weights), 4
            )
            if margins
            else None
        )

        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=self.name,
            model="granada-evidence-v1",
            answers=answers,
            confidence=overall,
            latency_ms=0,
            fallback_used=False,
            fallback_reason=(
                f"declined: no evidence for {', '.join(unanswered)}" if unanswered else None
            ),
            policy_result={"evidence": evidence, "unanswered": unanswered},
        )
