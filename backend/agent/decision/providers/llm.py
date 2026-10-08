"""LLM-backed structured decision provider.

Two jobs, and the second is the one people forget:

1. **Fallback.** When the local engine cannot answer, this answers the same questions through
   the model gateway's structured-output path, so ingestion and mail triage keep
   working instead of stopping at an outage.
2. **Second opinion.** For a decision in the MEDIUM band, an independent answer
   from a different model is a cheap verification - and one that only means
   something because the two take genuinely different routes to the answer.

Everything it returns is validated against Granada's own question definitions, so
an LLM cannot widen an option set either.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from agent.decision.models import Answer, DecisionRequest, DecisionResult, QuestionType
from agent.decision.exceptions import (
    DecisionProviderError,
    DecisionProviderUnavailable,
    InvalidDecisionResult,
)
from agent.decision.providers.base import BaseDecisionProvider

logger = logging.getLogger(__name__)

#: The synthesis tier is deliberately not used. Classification and bounded
#: judgement are the cheap tier's job; paying synthesis prices to decide whether
#: an email is an acknowledgement is the most common way an agent platform
#: becomes uneconomic.
DEFAULT_TIER = "CLASSIFICATION"

PROMPT_VERSION = "decision-structured-v1"

SYSTEM_PROMPT = (
    "You are a decision component inside a grant-management platform. "
    "You answer narrow, bounded questions about a supplied state. "
    "You do not write prose, you do not give advice, and you do not explain. "
    "You return a single JSON object and nothing else."
)


class LLMDecisionProvider(BaseDecisionProvider):
    """Answers structured questions through a text model."""

    name = "llm"

    def __init__(self, gateway: Optional[Any] = None, *, tier: str = DEFAULT_TIER) -> None:
        #: A ``agent.model_gateway.ModelGateway``. Required for availability,
        #: because an LLM provider with no gateway has nowhere to send anything.
        self.gateway = gateway
        self.tier = tier
        self.available = gateway is not None

    # ------------------------------------------------------------------
    def schema_for(self, request: DecisionRequest) -> dict[str, Any]:
        """The JSON schema the model must satisfy.

        Built from the question definitions rather than hand-written, so the
        option sets cannot drift from Granada's own validation - which is the
        check that actually runs.
        """
        properties: dict[str, Any] = {}
        for question in request.questions:
            if question.type == QuestionType.BOOLEAN:
                properties[question.key] = {"type": "boolean"}
            elif question.type == QuestionType.CHOICE:
                properties[question.key] = {"type": "string", "enum": list(question.options)}
            else:  # SCORE
                properties[question.key] = {
                    "type": "integer",
                    "minimum": question.minimum,
                    "maximum": question.maximum,
                }
        return {
            "type": "object",
            "required": [q.key for q in request.questions],
            "properties": properties,
            "additionalProperties": False,
        }

    def prompt_for(self, request: DecisionRequest) -> str:
        lines = [
            f"Decision type: {request.decision_type}",
            "",
            "State (JSON):",
            json.dumps(request.state, indent=2, default=str, sort_keys=True),
            "",
            "Answer every question below.",
        ]
        for question in request.questions:
            lines.append(f'- "{question.key}": {question.provider_hint()}')
        lines.append("")
        lines.append("Return one JSON object with exactly these keys: " + ", ".join(request.question_keys))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        from agent.decision.telemetry import timed

        if self.gateway is None:
            raise DecisionProviderUnavailable("no model gateway is configured for the llm provider")

        schema = self.schema_for(request)
        prompt = self.prompt_for(request)

        with timed() as clock:
            try:
                result = self.gateway.complete(
                    tier=self.tier,
                    prompt=prompt,
                    system=SYSTEM_PROMPT,
                    prompt_version=PROMPT_VERSION,
                    org_id=request.organisation_id,
                    response_schema=schema,
                    temperature=0.0,
                )
            except Exception as exc:
                # ModelGatewayError covers invalid output, budget and transport.
                # All of them are "this provider could not answer", which is what
                # the chain needs to know.
                raise DecisionProviderError(
                    f"model gateway refused: {type(exc).__name__}: {exc}"
                ) from exc

        if result.data is None:
            raise InvalidDecisionResult("the model returned no structured output")

        answers: dict[str, Answer] = {}
        for question in request.questions:
            if question.key not in result.data:
                raise InvalidDecisionResult(f"the model omitted {question.key!r}")
            answers[question.key] = Answer(
                key=question.key,
                value=question.validate(result.data[question.key]),
                # The model gateway's own confidence is not a calibrated
                # probability of this answer being right, so none is claimed.
                confidence=None,
            )

        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=self.name,
            model=result.model,
            answers=answers,
            confidence=None,
            latency_ms=clock.elapsed_ms,
            raw_provider_reference=result.invocation_id,
            correlation_id=request.correlation_id,
            question_schema_version=request.question_schema_version,
            provider_chain=(self.name,),
        )
