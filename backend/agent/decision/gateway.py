"""Decision Gateway: chains providers, applies policy, records everything.

The shape the brief requires
----------------------------

    SEARCH BOTS -> POSTGRES/REDIS -> NORMALISATION -> HARD RULES
                                                          |
                                                   DECISION GATEWAY
                                                    +-- LOCAL (Granada's own evidence engine)
                                                    +-- RULES
                                                    +-- LLM FALLBACK
                                                          |
                                                   POLICY / AUTHORITY ENGINE
                                                          |
                                                   WORKFLOW ENGINE -> AGENTS
                                                          |
                                                   APPROVAL GATES -> EXECUTION

Two properties are structural rather than documented:

**Nothing here has a side effect.** The gateway returns a
:class:`DecisionResult` and a :class:`PolicyOutcome`. There is no code path from a
provider to an email, a submission or a payment. The brief's forbidden pattern -
``decider -> directly submits grant`` - is not merely discouraged; there is no import
in this package that could do it.

**Shadow mode cannot influence anything, because its result is never returned as
the acting one.** A shadow run computes the provider's answer, records it beside
the baseline, and marks it ``shadow=True``. Callers that act on the returned
result are acting on the baseline, so a bug in the comparison cannot leak
authority.

Failing open, in the right direction
------------------------------------
A provider being down must not stop opportunity ingestion or mail triage. The chain
moves to the next provider, and when the chain is exhausted, the decision is
``UNKNOWN`` with ``requires_human=True`` - not a default answer. "The decision
layer is unavailable" must never silently become "the answer is yes", and it must
never become "the answer is no" either, because that would discard real
opportunities.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.decision.exceptions import (
    DecisionError,
    DecisionProviderError,
    DecisionProviderUnavailable,
    DecisionRefused,
    InvalidDecisionResult,
    NoDecisionProvider,
    PolicyBlocked,
)
from agent.decision.models import Answer, DecisionRequest, DecisionResult
from agent.decision.policy import (
    Autonomy,
    ConfidenceBand,
    DecisionPolicy,
    PolicyOutcome,
    RolloutStage,
    band_for,
    policy_for,
)
from agent.decision.telemetry import CircuitBreaker, record_decision, timed
from observability import metrics

logger = logging.getLogger(__name__)


class ProviderChain:
    """An ordered list of providers, tried in turn.

    Order encodes the ensemble policy: deterministic safety rules first, then the
    decision model, then a general model. A provider that is unavailable or
    circuit-broken is skipped without being called, which keeps an outage cheap.
    """

    def __init__(
        self,
        providers: Sequence[Any] = (),
        *,
        breakers: Optional[dict[str, CircuitBreaker]] = None,
    ) -> None:
        self.providers = list(providers)
        self.breakers = breakers if breakers is not None else {}

    def add(self, provider: Any) -> "ProviderChain":
        self.providers.append(provider)
        return self

    def breaker_for(self, name: str) -> CircuitBreaker:
        return self.breakers.setdefault(name, CircuitBreaker())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.providers)

    def attempt(
        self,
        request: DecisionRequest,
        *,
        timeout_seconds: int = 20,
        skip: Iterable[str] = (),
    ) -> tuple[DecisionResult, list[str]]:
        """Try each provider. Returns the first real answer and what was skipped."""
        skip = set(skip)
        attempted: list[str] = []
        last_error: Optional[Exception] = None

        for provider in self.providers:
            if provider.name in skip:
                attempted.append(provider.name)
                continue
            if not getattr(provider, "available", False):
                attempted.append(provider.name)
                metrics.inc("decision.provider_unavailable", provider=provider.name)
                continue

            breaker = self.breaker_for(provider.name)
            if breaker.is_open:
                attempted.append(provider.name)
                metrics.inc("decision.provider_unavailable", provider=provider.name, reason="circuit_open")
                continue

            try:
                result = provider.decide(request, timeout_seconds=timeout_seconds)
            except DecisionRefused as exc:
                # A refusal is information, not an outage: this provider has
                # nothing to say about this decision type. Move on, and do not
                # count it against the breaker's health.
                breaker.record_success()
                attempted.append(provider.name)
                last_error = exc
                continue
            except (DecisionProviderUnavailable, DecisionProviderError, InvalidDecisionResult) as exc:
                breaker.record_failure()
                attempted.append(provider.name)
                last_error = exc
                metrics.inc("decision.failed", provider=provider.name, reason=type(exc).__name__)
                logger.warning(
                    "decision_provider_failed",
                    extra={
                        "provider": provider.name,
                        "decision_type": request.decision_type,
                        "reason": type(exc).__name__,
                        "correlation_id": request.correlation_id,
                    },
                )
                continue
            except DecisionError as exc:
                breaker.record_failure()
                attempted.append(provider.name)
                last_error = exc
                continue

            breaker.record_success()
            attempted.append(provider.name)
            result.provider_chain = tuple(attempted)
            return result, attempted

        raise NoDecisionProvider(
            f"no provider could answer {request.decision_type!r}; tried {attempted}"
            + (f"; last error: {type(last_error).__name__}: {last_error}" if last_error else "")
        )


@dataclass
class Agreement:
    """Whether two providers agreed, per question.

    Recorded in shadow mode because "did the second opinion agree with our rules" is the entire
    question shadow mode exists to answer, and it cannot be answered from logs
    that only kept one side.
    """

    compared: tuple[str, ...]
    agreed: tuple[str, ...]
    disagreed: tuple[str, ...]

    @property
    def fully_agreed(self) -> bool:
        return not self.disagreed


def state_fingerprint(state: dict[str, Any]) -> str:
    """A stable hash of the decision state.

    Used for cache keys and for invalidating a recorded decision when the
    underlying profile or opportunity changes. Key-order independent, because two
    callers building the same state in a different order have the same state.
    """
    canonical = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compare(left: DecisionResult, right: DecisionResult) -> Agreement:
    """Per-question agreement between two results."""
    keys = [k for k in left.answers if k in right.answers]
    agreed = [k for k in keys if left.answers[k].value == right.answers[k].value]
    disagreed = [k for k in keys if left.answers[k].value != right.answers[k].value]
    return Agreement(compared=tuple(keys), agreed=tuple(agreed), disagreed=tuple(disagreed))


class DecisionGateway:
    """The only way Granada makes a bounded decision."""

    def __init__(
        self,
        *,
        chain: Optional[ProviderChain] = None,
        shadow_provider: Optional[Any] = None,
        policies: Optional[dict[str, DecisionPolicy]] = None,
        stage: str = RolloutStage.SHADOW,
        autonomy: str = Autonomy.MONITOR_ONLY,
        db: Optional[Session] = None,
        cache_ttl_seconds: int = 3600,
        timeout_seconds: int = 20,
        store_state: bool = False,
    ) -> None:
        self.chain = chain or ProviderChain()
        self.shadow_provider = shadow_provider
        #: SHADOW is the default. It is the only stage that cannot influence
        #: anything, and the brief is explicit that it comes first.
        self.stage = stage
        self.autonomy = autonomy
        self.policies = policies
        self.db = db
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        #: Whether to store the state itself. Default False: references and a
        #: fingerprint are enough for audit, and storing the state puts donor and
        #: beneficiary content in an ops table.
        self.store_state = store_state

    # ------------------------------------------------------------------
    # Policy: Granada owns permission
    # ------------------------------------------------------------------
    def evaluate_policy(
        self,
        result: DecisionResult,
        request: DecisionRequest,
        *,
        action: Optional[str] = None,
    ) -> PolicyOutcome:
        """Decide whether this answer may drive anything, and whether it does now.

        ``allowed`` and ``acted`` are separate on purpose. "Policy permits this"
        and "we are currently letting this happen" are different facts, and
        collapsing them would make the rollout stage invisible in the audit trail.
        """
        policy = policy_for(request.decision_type, self.policies)
        band = band_for(result.confidence)

        if action and action in RolloutStage.ALWAYS_HUMAN:
            return PolicyOutcome(
                allowed=False, acted=False, band=band, stage=self.stage,
                autonomy=self.autonomy, requires_human=True,
                reason=f"{action} always requires a human, at every autonomy level",
            )

        if action and action in policy.always_human_actions:
            return PolicyOutcome(
                allowed=False, acted=False, band=band, stage=self.stage,
                autonomy=self.autonomy, requires_human=True,
                reason=f"{action} requires a human for {request.decision_type}",
            )

        for flag in policy.human_if_context:
            if request.policy_context.get(flag) or request.state.get(flag):
                return PolicyOutcome(
                    allowed=False, acted=False, band=band, stage=self.stage,
                    autonomy=self.autonomy, requires_human=True,
                    reason=f"{flag} is set, which requires a human for {request.decision_type}",
                )

        minimum = request.minimum_confidence
        if minimum is None:
            minimum = (
                policy.minimum_for_external_action
                if policy.has_external_side_effect
                else policy.minimum_for_internal_action
            )

        if result.confidence is None:
            return PolicyOutcome(
                allowed=False, acted=False, band=ConfidenceBand.LOW, stage=self.stage,
                autonomy=self.autonomy, requires_human=True,
                reason=(
                    "the provider reported no confidence, so the answer cannot be "
                    "trusted to act on; an unreported confidence is not a high one"
                ),
            )

        if result.confidence < minimum:
            return PolicyOutcome(
                allowed=False, acted=False, band=band, stage=self.stage,
                autonomy=self.autonomy,
                requires_human=result.confidence < policy.minimum_confidence,
                reason=(
                    f"confidence {result.confidence:.3f} is below the {minimum:.2f} "
                    f"required for this action"
                ),
            )

        if not Autonomy.at_least(self.autonomy, policy.required_autonomy):
            return PolicyOutcome(
                allowed=False, acted=False, band=band, stage=self.stage,
                autonomy=self.autonomy,
                reason=(
                    f"the organisation's authority level {self.autonomy} is below "
                    f"{policy.required_autonomy} for {request.decision_type}"
                ),
            )

        # Permitted by policy. Whether it *acts* depends on the rollout stage, and
        # that separation is what makes shadow mode safe to leave on.
        acting = self.stage in RolloutStage.ACTING
        return PolicyOutcome(
            allowed=True,
            acted=acting,
            band=band,
            stage=self.stage,
            autonomy=self.autonomy,
            requires_human=False,
            shadowed=not acting,
            reason=(
                f"confidence {result.confidence:.3f} in band {band}, authority "
                f"{self.autonomy} sufficient"
                + ("" if acting else f"; rollout stage {self.stage} records but does not act")
            ),
        )

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------
    def cache_lookup(self, request: DecisionRequest, provider_name: str) -> Optional[DecisionResult]:
        """A cached result for an identical decision, if one is still valid.

        The key includes the question schema version and the provider, so a
        changed option set or a provider switch cannot serve a stale answer. The
        state fingerprint covers profile and opportunity changes, which is what
        makes an edit invalidate rather than linger.
        """
        if self.db is None:
            return None
        fingerprint = state_fingerprint(request.state)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.cache_ttl_seconds)
        row = self.db.execute(
            select(models_module().DecisionRecord).where(
                models_module().DecisionRecord.organisation_id == request.organisation_id,
                models_module().DecisionRecord.decision_type == request.decision_type,
                models_module().DecisionRecord.state_hash == fingerprint,
                models_module().DecisionRecord.question_schema_version
                == request.question_schema_version,
                models_module().DecisionRecord.provider == provider_name,
                models_module().DecisionRecord.fallback_used.is_(False),
                models_module().DecisionRecord.created_at >= cutoff,
            ).order_by(models_module().DecisionRecord.created_at.desc())
        ).scalars().first()
        if row is None:
            return None
        metrics.inc("decision.cache_hit", decision_type=request.decision_type)
        answers = {
            key: Answer(key=key, value=value)
            for key, value in (row.answers or {}).items()
        }
        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=row.provider,
            model=row.model,
            answers=answers,
            confidence=row.confidence,
            latency_ms=0,
            correlation_id=request.correlation_id,
            question_schema_version=request.question_schema_version,
        )

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------
    def decide(
        self, request: DecisionRequest, *, action: Optional[str] = None
    ) -> DecisionResult:
        """Answer the request, apply policy, and record it."""
        metrics.inc("decision.requested", decision_type=request.decision_type)

        cached = self.cache_lookup(request, self.chain.names[0] if self.chain.names else "")
        if cached is not None and self.stage in RolloutStage.ACTING:
            # A cache hit is only usable when we are acting at all. Replaying a
            # cached answer into an advisory run would misreport the provider.
            cached.policy_result = self.evaluate_policy(cached, request, action=action).as_dict()
            return cached
        if cached is None:
            metrics.inc("decision.cache_miss", decision_type=request.decision_type)

        with timed() as clock:
            result, attempted = self.chain.attempt(request, timeout_seconds=self.timeout_seconds)
            result.latency_ms = result.latency_ms or clock.elapsed_ms
            result.provider_chain = tuple(attempted)

        outcome = self.evaluate_policy(result, request, action=action)
        result.policy_result = outcome.as_dict()
        result.fallback_used = len(result.provider_chain) > 1
        if result.fallback_used:
            result.fallback_reason = f"chain attempted {list(result.provider_chain)}"

        if not outcome.allowed:
            metrics.inc("decision.policy_blocked", decision_type=request.decision_type)
            if outcome.requires_human:
                metrics.inc("decision.escalated", decision_type=request.decision_type)
        if outcome.band == ConfidenceBand.LOW or result.confidence is None:
            metrics.inc("decision.low_confidence", decision_type=request.decision_type)

        record_decision(result, shadow=outcome.shadowed, fallback=result.fallback_used)
        self._record(result, request)
        return result

    def decide_with_shadow(
        self, request: DecisionRequest, *, action: Optional[str] = None
    ) -> tuple[DecisionResult, Optional[DecisionResult], Optional[Agreement]]:
        """The acting decision, plus what the shadow provider would have said.

        Returned as a triple rather than merged, because merging is how a shadow
        answer ends up in the field a caller reads. The first element is the only
        one anything may act on.
        """
        acting = self.decide(request, action=action)
        if self.shadow_provider is None or not getattr(self.shadow_provider, "available", False):
            return acting, None, None

        try:
            shadow = self.shadow_provider.decide(request, timeout_seconds=self.timeout_seconds)
        except DecisionError as exc:
            logger.info(
                "shadow_decision_unavailable",
                extra={
                    "provider": getattr(self.shadow_provider, "name", "unknown"),
                    "reason": type(exc).__name__,
                    "correlation_id": request.correlation_id,
                },
            )
            return acting, None, None

        # Marked, always. This is the flag that says "recorded, not acted on".
        shadow.shadow = True
        shadow.policy_result = {
            "shadow": True,
            "reason": "recorded for comparison; this result influenced nothing",
            "acting_provider": acting.provider,
        }
        agreement = compare(acting, shadow)
        metrics.inc("decision.agreement" if agreement.fully_agreed else "decision.disagreement",
                    acting_provider=acting.provider, shadow_provider=shadow.provider)
        self._record(shadow, request, shadow_of=acting.decision_id)
        return acting, shadow, agreement

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _record(
        self,
        result: DecisionResult,
        request: DecisionRequest,
        *,
        shadow_of: Optional[str] = None,
    ) -> Optional[Any]:
        if self.db is None:
            return None
        models = models_module()
        # A shadow record needs its own row identity. ``decision_id`` identifies
        # the *request* - a provider echoes it back - so reusing it would make the
        # acting record and its shadow collide on the primary key. The link
        # between them is ``shadow_of``, which is the relationship that actually
        # means something.
        record_id = result.decision_id if shadow_of is None else str(_uuid())
        row = models.DecisionRecord(
            id=record_id,
            tenant_id=request.tenant_id,
            organisation_id=request.organisation_id,
            decision_type=request.decision_type,
            subject_type=_subject_type(request),
            subject_id=_subject_id(request),
            workflow_id=request.workflow_id,
            provider=result.provider,
            model=result.model,
            question_schema_version=request.question_schema_version,
            state_hash=state_fingerprint(request.state),
            # Only when explicitly enabled: a fingerprint is enough for audit and
            # does not put donor content in an ops table.
            state_snapshot=request.state if self.store_state else None,
            answers={k: a.value for k, a in result.answers.items()},
            confidences={
                k: a.confidence for k, a in result.answers.items() if a.confidence is not None
            },
            confidence=result.confidence,
            probabilities={
                k: a.distribution for k, a in result.answers.items() if a.distribution
            } or None,
            policy_outcome=(result.policy_result or {}).get("allowed"),
            policy_detail=result.policy_result,
            fallback_used=result.fallback_used,
            latency_ms=result.latency_ms,
            shadow=result.shadow,
            shadow_of=shadow_of,
            correlation_id=result.correlation_id,
            created_at=result.created_at,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=self.cache_ttl_seconds)
                if self.cache_ttl_seconds
                else None
            ),
        )
        self.db.add(row)
        self.db.flush()
        return row


def _subject_type(request: DecisionRequest) -> Optional[str]:
    if request.opportunity_id:
        return "OPPORTUNITY"
    if request.application_id:
        return "APPLICATION"
    if request.workflow_id:
        return "WORKFLOW"
    return None


def _subject_id(request: DecisionRequest) -> Optional[str]:
    return request.opportunity_id or request.application_id or request.workflow_id


def _uuid() -> str:
    import uuid as _uuid_module

    return str(_uuid_module.uuid4())


def models_module():
    """Late import.

    The gateway is imported by ``agent.decision``, which the models module does
    not import back - but keeping this lazy means the decision layer can be
    imported in a context where the ORM is not yet configured, which the tests
    rely on.
    """
    import models

    return models


# ---------------------------------------------------------------------------
# Construction from settings
# ---------------------------------------------------------------------------
def build_gateway(
    *,
    db: Optional[Session] = None,
    settings: Optional[Any] = None,
    model_gateway: Optional[Any] = None,
    providers: Optional[Sequence[Any]] = None,
    **overrides: Any,
) -> DecisionGateway:
    """Build the configured gateway.

    Never raises for missing configuration, and has NO external dependency to be missing: the
    default chain is ``rules -> local``, both of which are part of Granada. A self-hosted
    deployment answers every decision it needs with no account, no key and no network.

    The previous chain put a vendor provider here. The brief asks for a provider-neutral gateway
    and forbids coupling workflows to one LLM vendor; a vendor is one way to satisfy that, and a
    self-contained engine is a better one, because it cannot be rate-limited, cannot go down, and
    costs nothing per decision.

    ``llm`` remains available and is still opt-in, so a deployment that WANTS a model for prose
    synthesis can have one - behind the same interface, in the same slot.
    """
    from agent.decision.providers.llm import LLMDecisionProvider
    from agent.decision.providers.local import LocalDecisionProvider
    from agent.decision.providers.rules import RulesDecisionProvider, default_rules

    if providers is not None:
        chain = ProviderChain(list(providers))
    else:
        provider_name = getattr(settings, "decision_provider", "rules") if settings else "rules"

        rules = RulesDecisionProvider(default_rules())
        local = LocalDecisionProvider()
        llm = LLMDecisionProvider(model_gateway) if model_gateway is not None else None

        # ORDER IS THE POLICY: deterministic gates first, then Granada's own evidence engine,
        # then - only if configured - a model.
        chain = ProviderChain([rules, local])
        if llm is not None and provider_name in {"llm", "hybrid"}:
            chain.add(llm)

    stage = getattr(settings, "decision_rollout_stage", RolloutStage.SHADOW) if settings else RolloutStage.SHADOW
    autonomy = getattr(settings, "decision_autonomy", Autonomy.MONITOR_ONLY) if settings else Autonomy.MONITOR_ONLY

    # No shadow vendor provider.
    #
    # The shadow slot used to hold an external engine, so SHADOW mode recorded "what the vendor
    # would have decided" alongside Granada's own answer. With the vendor gone there is nothing to
    # shadow: `rules` and `local` are both Granada's, and they run in the CHAIN rather than beside
    # it. Inventing a shadow here would be recording Granada arguing with itself and calling it
    # independent evidence.
    #
    # The slot remains in the gateway, so a deployment that later adds a second opinion - a model,
    # or another engine - attaches it here without changing anything else.
    shadow_provider = None

    kwargs: dict[str, Any] = {
        "chain": chain,
        "shadow_provider": shadow_provider,
        "stage": stage,
        "autonomy": autonomy,
        "db": db,
        "cache_ttl_seconds": getattr(settings, "decision_cache_ttl_seconds", 3600) if settings else 3600,
        "timeout_seconds": getattr(settings, "decision_timeout_seconds", 20) if settings else 20,
        "store_state": bool(getattr(settings, "decision_store_state", False)) if settings else False,
    }
    kwargs.update(overrides)
    return DecisionGateway(**kwargs)
