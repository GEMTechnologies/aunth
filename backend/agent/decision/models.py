"""Canonical decision request and result models.

These are Granada's own types. Nothing here is a vendor class, and that is the
point: the brief requires that Granada not become dependent on any one provider, and the way
a dependency creeps in is by letting a vendor's types become the vocabulary of
the business layer. A provider maps these onto its own types.
``Score`` internally and maps the answers back; no other module ever sees a
vendor name.

Question types are deliberately three
-------------------------------------
``BOOLEAN``, ``CHOICE`` and ``SCORE``. They map cleanly onto the
``Noul``/``Choice``/``Score`` and onto anything an LLM can be asked to emit as
JSON, so a provider change is a mapping change rather than a redesign.

The brief's own catalogue of decisions - eligibility, email type, approval
needed, risk level, which action next - all reduce to those three shapes. That is
not a coincidence: a decision that needs a fourth shape is usually a decision
that has not been narrowed enough yet, and the brief is explicit that broad
questions ("what should we do with this grant?") must be broken into narrow
judgments.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class QuestionType:
    """Granada's question vocabulary. Three shapes, no vendor types."""

    BOOLEAN = "BOOLEAN"   # yes/no, mapped to a boolean-like answer type
    CHOICE = "CHOICE"     # one of a closed set
    SCORE = "SCORE"       # a bounded integer

    ALL = frozenset({BOOLEAN, CHOICE, SCORE})


#: The closed set of email intents from the brief. A choice question's options
#: are always an explicit allowlist, so a model cannot invent an actor name - the
#: same reasoning as the agent-routing allowlist.
EMAIL_INTENTS = (
    "ACKNOWLEDGEMENT",
    "CLARIFICATION_REQUEST",
    "DOCUMENT_REQUEST",
    "DEADLINE_CHANGE",
    "INTERVIEW_INVITATION",
    "AWARD_NOTICE",
    "REJECTION_NOTICE",
    "CONTRACT",
    "PAYMENT_OR_BANK_REQUEST",
    "GENERAL_QUESTION",
    "BOUNCE",
    "UNKNOWN",
)

#: The closed set of agents an event may be routed to. Routing must never accept
#: an arbitrary class or function name from model output.
AGENT_ROUTES = (
    "NO_ACTION",
    "MATCHING",
    "DONOR_RESEARCH",
    "PROPOSAL",
    "BUDGET",
    "COMPLIANCE",
    "DOCUMENT",
    "EMAIL",
    "FOLLOW_UP",
    "SUBMISSION",
    "HUMAN_REVIEW",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class DecisionQuestion:
    """One narrow question, with its own closed option set where applicable."""

    key: str
    type: str
    instructions: str
    #: For CHOICE. An allowlist, not a hint.
    options: tuple[str, ...] = ()
    #: For SCORE. Inclusive bounds.
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    def __post_init__(self) -> None:
        if self.type not in QuestionType.ALL:
            from agent.decision.exceptions import UnknownQuestionType

            raise UnknownQuestionType(
                f"question {self.key!r} has type {self.type!r}; "
                f"expected one of {sorted(QuestionType.ALL)}"
            )
        if self.type == QuestionType.CHOICE and not self.options:
            raise ValueError(f"choice question {self.key!r} needs options")
        if self.type == QuestionType.SCORE and (
            self.minimum is None or self.maximum is None
        ):
            raise ValueError(f"score question {self.key!r} needs minimum and maximum")

    def validate(self, value: Any) -> Any:
        """Check one answer against this question. Raises rather than coercing."""
        from agent.decision.exceptions import InvalidDecisionResult

        if self.type == QuestionType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().upper() in {"YES", "TRUE", "NO", "FALSE"}:
                return value.strip().upper() in {"YES", "TRUE"}
            raise InvalidDecisionResult(
                f"{self.key}: expected a yes/no answer, got {value!r}"
            )

        if self.type == QuestionType.CHOICE:
            if not isinstance(value, str):
                raise InvalidDecisionResult(
                    f"{self.key}: expected one of {list(self.options)}, got {value!r}"
                )
            candidate = value.strip().upper()
            if candidate not in self.options:
                # Deliberately not "nearest option". See InvalidDecisionResult.
                raise InvalidDecisionResult(
                    f"{self.key}: {value!r} is not one of {list(self.options)}"
                )
            return candidate

        # SCORE
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidDecisionResult(
                f"{self.key}: expected a number between {self.minimum} and "
                f"{self.maximum}, got {value!r}"
            )
        if not (self.minimum <= float(value) <= self.maximum):
            raise InvalidDecisionResult(
                f"{self.key}: {value} is outside {self.minimum}..{self.maximum}"
            )
        return float(value)

    def provider_hint(self) -> str:
        """The option list as a prompt fragment, for providers that need prose."""
        if self.type == QuestionType.CHOICE:
            return f"{self.instructions} Answer with exactly one of: {', '.join(self.options)}."
        if self.type == QuestionType.BOOLEAN:
            return f"{self.instructions} Answer YES or NO."
        return (
            f"{self.instructions} Answer with a whole number from "
            f"{self.minimum} to {self.maximum}."
        )


@dataclass
class DecisionRequest:
    """What Granada asks for. Carries references, never bulk sensitive content."""

    decision_type: str
    questions: tuple[DecisionQuestion, ...]
    #: The minimum required state. Built per decision type; never "send everything".
    state: dict[str, Any]
    tenant_id: Optional[str] = None
    organisation_id: Optional[str] = None
    application_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    workflow_id: Optional[str] = None
    #: "" means "whatever the configured chain says".
    requested_provider: Optional[str] = None
    minimum_confidence: Optional[float] = None
    policy_context: dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    decision_id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=_now)
    #: Bumped when a question's meaning or option set changes, so cached and
    #: recorded decisions from an older schema are never reused.
    question_schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.decision_type:
            raise ValueError("decision_type is required")
        if not self.questions:
            raise ValueError("a decision requires at least one question")
        keys = [q.key for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate question keys: {sorted(keys)}")

    def question(self, key: str) -> DecisionQuestion:
        for q in self.questions:
            if q.key == key:
                return q
        raise KeyError(key)

    @property
    def question_keys(self) -> tuple[str, ...]:
        return tuple(q.key for q in self.questions)


@dataclass
class Answer:
    """One validated answer, with whatever confidence the provider offered."""

    key: str
    value: Any
    confidence: Optional[float] = None
    #: Distribution over the options where the provider supplies one. Optional,
    #: because a provider's surface may return the chosen value and Granada
    #: must not pretend a probability exists when it was not given one.
    distribution: Optional[dict[str, float]] = None

    @property
    def as_yes_no(self) -> Optional[bool]:
        return self.value if isinstance(self.value, bool) else None


@dataclass
class DecisionResult:
    """What came back, and how much Granada is entitled to trust it."""

    decision_id: str
    decision_type: str
    provider: str
    model: Optional[str]
    answers: dict[str, Answer]
    #: The lowest per-answer confidence, or the provider's overall figure.
    #: ``None`` when the provider supplied none - which is a real case and must
    #: not be rendered as 1.0 or as 0.0.
    confidence: Optional[float]
    latency_ms: int = 0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    policy_result: Optional[dict[str, Any]] = None
    #: A provider-side identifier or trace reference, when it is safe to keep.
    raw_provider_reference: Optional[str] = None
    #: True when the gateway ran this only to compare, and nothing may act on it.
    shadow: bool = False
    created_at: datetime = field(default_factory=_now)
    correlation_id: Optional[str] = None
    question_schema_version: str = "v1"
    provider_chain: tuple[str, ...] = ()

    def value(self, key: str) -> Any:
        answer = self.answers.get(key)
        return None if answer is None else answer.value

    def confidence_for(self, key: str) -> Optional[float]:
        answer = self.answers.get(key)
        return None if answer is None else answer.confidence

    @property
    def is_confident(self) -> bool:
        return self.confidence is not None and self.confidence >= 0.95

    def audit_summary(self) -> dict[str, Any]:
        """The shape the brief requires in an audit entry.

        Named fields with the answer, the confidence, the provenance and the
        correlation id - not the sentence "AI decided yes".
        """
        return {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "provider": self.provider,
            "model": self.model,
            "answers": {k: a.value for k, a in self.answers.items()},
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "shadow": self.shadow,
            "policy_result": self.policy_result,
            "correlation_id": self.correlation_id,
            "question_schema_version": self.question_schema_version,
        }
