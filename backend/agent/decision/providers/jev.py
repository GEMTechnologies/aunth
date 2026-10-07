"""Jev / TypeSafe System One provider.

Narrow decisions only
---------------------
Jev is **not** the proposal writer, the researcher, the email writer, or the
grant-writing model. It answers fast, bounded, structured questions. Prose and
long-form reasoning stay with ordinary LLMs. A provider that starts being asked
to write a needs statement has been misused, and the docstring says so because
that is the misuse that will actually happen.

Verified against the real SDK
-----------------------------
This mapping was written against the official SDK's published interface, read
from the vendor's own repository and documentation rather than guessed:

    from typesafe_sdk import Choice, Noul, Score, TypeSafeClient
    client.system_one(state, {"key": Noul(instructions=...)})
    result.nouls["key"].noul / result.choices["key"].choice / result.scores["key"].score

Three details are worth recording because they are easy to get wrong:
  * the boolean-like question type is **``Noul``**, not ``Bool``;
  * ``state`` may be a string or a mapping;
  * the answer is read from a **type-specific collection** (``.nouls``,
    ``.choices``, ``.scores``) rather than one uniform ``.answers`` mapping.

Granada's own types are ``BOOLEAN``/``CHOICE``/``SCORE``, and the mapping lives
**here and nowhere else**. That is what keeps the vendor's class names out of the
business layer, so swapping or removing Jev is a change to one file.

What this provider deliberately does not claim
----------------------------------------------
The published SDK surface shows the chosen value. It does **not** document a
per-answer confidence or a probability distribution. So this provider reports
``confidence=None`` unless the response genuinely carries one, and the gateway
treats a missing confidence as the LOW band.

That is not pedantry. Fabricating a confidence - or assuming 1.0 because the
answer parsed - would let an unmeasured provider reach the VERY_HIGH band and
thereby qualify for autonomy it has not earned. The brief's instruction to build
Granada-specific evaluation data exists precisely because vendor benchmark claims
are not calibration.
"""

from __future__ import annotations

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


class JevDecisionProvider(BaseDecisionProvider):
    """Talks to TypeSafe System One. Optional, and optional by design."""

    name = "jev"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "jev-latest",
        client_factory: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url or ""
        self.model = model or "jev-latest"
        #: Injected in tests so the ordinary suite never needs a paid API call.
        self._client_factory = client_factory
        #: Not an error when absent. The application must boot, ingest
        #: opportunities and triage mail with no TypeSafe key configured - the
        #: brief requires exactly that.
        self.available = bool(self.api_key) or client_factory is not None
        self._client: Any = None

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------
    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                api_key=self.api_key, base_url=self.base_url, model=self.model
            )

        try:
            from typesafe_sdk import TypeSafeClient
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise DecisionProviderUnavailable(
                "the official typesafe-sdk package is not installed; install "
                "'typesafe-sdk' or set DECISION_PROVIDER=rules. Granada must "
                "continue to operate without it."
            ) from exc

        # The SDK reads TYPESAFE_API_KEY from the environment itself, but the key
        # is passed explicitly where the constructor allows it so that the secret
        # layer is the single source and nothing depends on ambient state.
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            return TypeSafeClient(**kwargs)
        except TypeError:
            # Older or newer SDKs may not accept every keyword. Falling back to
            # the environment-configured client is correct behaviour; guessing
            # at a signature is not.
            return TypeSafeClient()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # ------------------------------------------------------------------
    # Mapping: Granada -> provider
    # ------------------------------------------------------------------
    def build_questions(self, request: DecisionRequest) -> dict[str, Any]:
        """Map Granada's questions to the SDK's question objects.

        This is the only place TypeSafe class names appear in Granada.
        """
        try:
            from typesafe_sdk import Choice, Noul, Score
        except ImportError as exc:  # pragma: no cover
            raise DecisionProviderUnavailable(
                "the official typesafe-sdk package is not installed"
            ) from exc

        questions: dict[str, Any] = {}
        for question in request.questions:
            if question.type == QuestionType.BOOLEAN:
                # ``Noul`` is the SDK's boolean-like type. ``criteria`` is not
                # required for it.
                questions[question.key] = Noul(instructions=question.instructions)
            elif question.type == QuestionType.CHOICE:
                # A closed option set, expressed as criteria. The model cannot
                # return an option outside this mapping, which is the property
                # the agent-routing allowlist depends on.
                questions[question.key] = Choice(
                    instructions=question.instructions,
                    criteria={option: None for option in question.options},
                )
            else:  # SCORE
                questions[question.key] = Score(
                    instructions=question.instructions,
                    criteria=[str(n) for n in range(question.minimum, question.maximum + 1)],
                )
        return questions

    def build_state(self, request: DecisionRequest) -> dict[str, Any]:
        """Minimise the state before it leaves the building.

        Redaction runs here as well as at the gateway, on the principle that the
        last step before transmission is the one that must not depend on an
        earlier caller having remembered.
        """
        from agent.redaction import redact

        safe: dict[str, Any] = {}
        for key, value in request.state.items():
            if isinstance(value, str):
                safe[key] = redact(value).text
            else:
                safe[key] = value
        # References only. A provider needs to know *which* opportunity, not the
        # organisation's whole file.
        for ref_key in ("opportunity_id", "application_id", "workflow_id"):
            ref = getattr(request, ref_key, None)
            if ref:
                safe.setdefault(ref_key, ref)
        return safe

    def model_for(self, request: DecisionRequest) -> str:
        return str(request.policy_context.get("model") or self.model)

    # ------------------------------------------------------------------
    # Mapping: provider -> Granada
    # ------------------------------------------------------------------
    def read_answers(self, request: DecisionRequest, response: Any) -> dict[str, Answer]:
        """Read the SDK's type-specific collections back into Granada answers."""
        answers: dict[str, Answer] = {}

        for question in request.questions:
            raw, confidence, distribution = self._extract(question, response)
            if raw is None:
                continue
            # Validated against Granada's own question definition, so a provider
            # answer outside the allowlist raises rather than being stored.
            validated = question.validate(raw)
            answers[question.key] = Answer(
                key=question.key,
                value=validated,
                confidence=confidence,
                distribution=distribution,
            )
        return answers

    @staticmethod
    def _extract(question: Any, response: Any) -> tuple[Any, Optional[float], Optional[dict[str, float]]]:
        """Pull one answer out of the SDK response.

        Tolerant about *where* the SDK puts things, strict about *what* comes
        back: a shape mismatch yields no answer (so the gateway falls through),
        whereas a value outside the option set raises during validation.
        """
        collection_name = {
            QuestionType.BOOLEAN: "nouls",
            QuestionType.CHOICE: "choices",
            QuestionType.SCORE: "scores",
        }[question.type]
        attribute_name = {
            QuestionType.BOOLEAN: "noul",
            QuestionType.CHOICE: "choice",
            QuestionType.SCORE: "score",
        }[question.type]

        collection = getattr(response, collection_name, None)
        if collection is None:
            return None, None, None
        try:
            entry = collection[question.key]
        except (KeyError, TypeError, IndexError):
            return None, None, None

        if entry is None:
            return None, None, None

        raw = getattr(entry, attribute_name, None)
        if raw is None:
            return None, None, None

        confidence = _float_or_none(getattr(entry, "confidence", None))
        probabilities = getattr(entry, "probabilities", None) or getattr(entry, "distribution", None)
        distribution: Optional[dict[str, float]] = None
        if isinstance(probabilities, dict):
            distribution = {
                str(k): float(v) for k, v in probabilities.items() if _float_or_none(v) is not None
            }
        return raw, confidence, distribution

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------
    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        from agent.decision.telemetry import timed

        questions = self.build_questions(request)
        state = self.build_state(request)

        with timed() as clock:
            try:
                response = self.client.system_one(state, questions)
            except Exception as exc:
                # The SDK's exception hierarchy is not part of what Granada
                # depends on, so it is normalised here into one retryable type.
                # Rate limits, timeouts and transport errors are all
                # "unavailable"; a 4xx caused by a malformed request is a
                # provider error, and both are handled by the chain.
                raise DecisionProviderError(
                    f"system_one call failed: {type(exc).__name__}: {exc}"
                ) from exc

        answers = self.read_answers(request, response)
        if not answers:
            raise InvalidDecisionResult(
                "the provider returned no readable answers for "
                f"{list(request.question_keys)}"
            )

        confidences = [a.confidence for a in answers.values() if a.confidence is not None]
        # None when the provider gave none. Not 1.0, and not 0.0.
        overall = min(confidences) if confidences else None

        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=self.name,
            model=self.model_for(request),
            answers=answers,
            confidence=overall,
            latency_ms=clock.elapsed_ms,
            raw_provider_reference=_safe_reference(response),
            correlation_id=request.correlation_id,
            question_schema_version=request.question_schema_version,
            provider_chain=(self.name,),
        )


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_reference(response: Any) -> Optional[str]:
    """A provider-side id, if it is a short opaque token.

    Long values are dropped rather than truncated: a "reference" that is actually
    a serialised response is a payload, and payloads do not belong in an audit
    column.
    """
    for attribute in ("id", "request_id", "response_id"):
        value = getattr(response, attribute, None)
        if isinstance(value, str) and 0 < len(value) <= 128:
            return value
    return None
