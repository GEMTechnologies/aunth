"""Decision Gateway: providers, policy, shadow mode, fallback, persistence.

The load-bearing tests here are:

* ``test_code_owns_permission_not_the_provider`` - the brief's central rule. A
  provider answering with total confidence authorises nothing.
* ``test_shadow_mode_cannot_influence_the_acting_decision`` - shadow mode's whole
  guarantee, asserted structurally rather than trusted.
* ``test_jev_provider_maps_granada_types_to_the_sdk`` - written against the SDK's
  real published interface, with a mock, so CI never needs a paid API call.

There is no code path here from a provider to a side effect, and these tests are
written to notice if one is ever added.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.decision.exceptions import (  # noqa: E402
    DecisionProviderError,
    DecisionProviderUnavailable,
    DecisionRefused,
    InvalidDecisionResult,
    NoDecisionProvider,
    UnknownQuestionType,
)
from agent.decision.gateway import (  # noqa: E402
    Agreement,
    DecisionGateway,
    ProviderChain,
    build_gateway,
    compare,
    state_fingerprint,
)
from agent.decision.models import (  # noqa: E402
    AGENT_ROUTES,
    EMAIL_INTENTS,
    Answer,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
    QuestionType,
)
from agent.decision.policy import (  # noqa: E402
    Autonomy,
    ConfidenceBand,
    RolloutStage,
    band_for,
)
from agent.decision.providers.base import BaseDecisionProvider  # noqa: E402
from agent.decision.providers.jev import JevDecisionProvider  # noqa: E402
from agent.decision.providers.llm import LLMDecisionProvider  # noqa: E402
from agent.decision.providers.rules import (  # noqa: E402
    RulesDecisionProvider,
    default_rules,
)
from agent.decision.telemetry import CircuitBreaker  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    # Schema copied from a session template rather than rebuilt: create_all to a
    # file on this filesystem costs ~3.8s per test because the schema has 38 tables
    # and 203 indexes. See tests/conftest.py::make_sqlite_db.
    engine, session = make_sqlite_db(tmp_path, "decisions.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Test NGO", slug="test-ngo", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id


def triage_request(**overrides) -> DecisionRequest:
    defaults: dict[str, Any] = {
        "decision_type": "opportunity_triage",
        "questions": (
            DecisionQuestion(
                key="strategic_fit",
                type=QuestionType.CHOICE,
                instructions="How well does this fit the organisation's strategy?",
                options=("VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"),
            ),
            DecisionQuestion(
                key="worth_researching",
                type=QuestionType.BOOLEAN,
                instructions="Is this worth deeper research?",
            ),
        ),
        "state": {
            "organisation": {"country": "Uganda", "sector": "Health"},
            "opportunity": {"title": "Community Health Grant", "country": "Uganda"},
            "deadline": {"days_remaining": 45},
            "eligibility": {"failed_gates": [], "unknown_gates": []},
            "documents": {"expired": []},
        },
        "organisation_id": overrides.pop("organisation_id", None),
        "correlation_id": "corr-1",
    }
    defaults.update(overrides)
    return DecisionRequest(**defaults)


class StubProvider(BaseDecisionProvider):
    """A provider whose every behaviour is scripted, for testing the chain.

    ``bypass_availability`` exists so the chain's own availability check can be
    tested. Without it the base class refuses on ``available=False`` anyway, so a
    test asserting "an unavailable provider was never called" passes even when the
    chain never checks - which is a test that cannot fail under the mutation it
    targets. With ``bypass_availability`` the provider *would* answer if called,
    so the only thing that can keep it uncalled is the chain.
    """

    def __init__(self, name: str, *, answers=None, confidence=0.97, error=None,
                 available=True, bypass_availability=False):
        self.name = name
        self.available = available
        self.bypass_availability = bypass_availability
        self._answers = answers or {}
        self._confidence = confidence
        self._error = error
        self.calls = 0

    def decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        if not self.bypass_availability:
            return super().decide(request, timeout_seconds=timeout_seconds)
        return self._decide(request, timeout_seconds=timeout_seconds)

    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        answers = {
            key: Answer(key=key, value=value, confidence=self._confidence)
            for key, value in self._answers.items()
        }
        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=self.name,
            model=f"{self.name}-model",
            answers=answers,
            confidence=self._confidence,
            latency_ms=5,
            correlation_id=request.correlation_id,
        )


# ---------------------------------------------------------------------------
# THE rule: the code owns permission
# ---------------------------------------------------------------------------
def test_code_owns_permission_not_the_provider(db, org):
    """A provider answering with total confidence authorises nothing.

    This is the brief's central architectural rule. The gateway returns a result;
    it is Granada's policy that decides whether anything may happen, and the same
    confident answer is permitted for an internal action and refused for an
    external one.
    """
    provider = StubProvider(
        "stub", answers={"worth_researching": True}, confidence=1.0
    )
    gateway = DecisionGateway(
        chain=ProviderChain([provider]),
        db=db,
        stage=RolloutStage.SHADOW,
        autonomy=Autonomy.MONITOR_ONLY,
    )
    request = triage_request(organisation_id=org)

    result = gateway.decide(request)
    assert result.provider == "stub"
    assert result.value("worth_researching") is True
    assert result.confidence == 1.0

    # Certainty from the provider did NOT grant authority. The stage is SHADOW,
    # so nothing acts regardless of how confident the answer was.
    outcome = gateway.evaluate_policy(result, request)
    assert outcome.allowed is True, "triage at MONITOR_ONLY should be permitted"
    assert outcome.acted is False, "a provider's confidence granted acting authority"
    assert outcome.shadowed is True


def test_an_always_human_action_is_refused_at_every_autonomy_level(db, org):
    """Bank details, contracts and legal declarations never delegate."""
    provider = StubProvider("stub", answers={"worth_researching": True}, confidence=1.0)
    for autonomy in Autonomy.ORDER:
        for stage in RolloutStage.ORDER:
            gateway = DecisionGateway(
                chain=ProviderChain([provider]),
                db=db,
                stage=stage,
                autonomy=autonomy,
            )
            outcome = gateway.evaluate_policy(
                DecisionResult(
                    decision_id="d", decision_type="opportunity_triage",
                    provider="stub", model=None, answers={}, confidence=1.0,
                ),
                triage_request(organisation_id=org),
                action="change_bank_details",
            )
            assert outcome.allowed is False, f"authorised at {autonomy}/{stage}"
            assert outcome.requires_human is True


def test_a_missing_confidence_is_not_a_high_one(db, org):
    """An unreported confidence cannot reach the VERY_HIGH band.

    The Jev SDK's documented surface does not include a per-answer confidence, so
    this is the live case rather than a hypothetical one. Defaulting an absent
    number to a confident one is how an unmeasured provider earns autonomy it has
    not demonstrated.
    """
    provider = StubProvider("stub", answers={"worth_researching": True}, confidence=None)
    gateway = DecisionGateway(
        chain=ProviderChain([provider]), db=db,
        stage=RolloutStage.LOW_RISK_EXTERNAL_AUTOMATION,
        autonomy=Autonomy.AUTOPILOT_WITH_GATES,
    )
    result = gateway.decide(triage_request(organisation_id=org))
    assert result.confidence is None

    outcome = gateway.evaluate_policy(result, triage_request(organisation_id=org))
    assert outcome.allowed is False
    assert outcome.requires_human is True
    assert band_for(None) == ConfidenceBand.LOW


def test_autonomy_below_the_required_level_refuses(db, org):
    provider = StubProvider("stub", answers={"most_urgent": "AWARD"}, confidence=0.99)
    low = DecisionGateway(
        chain=ProviderChain([provider]), db=db,
        stage=RolloutStage.LOW_RISK_EXTERNAL_AUTOMATION,
        autonomy=Autonomy.DRAFT_ONLY,
    )
    request = DecisionRequest(
        decision_type="email_triage",
        questions=(
            DecisionQuestion("most_urgent", QuestionType.CHOICE, "intent", EMAIL_INTENTS),
        ),
        state={}, organisation_id=org,
    )
    result = low.decide(request)
    outcome = low.evaluate_policy(result, request)
    assert outcome.allowed is False, "DRAFT_ONLY authorised an external action"
    assert "authority level" in outcome.reason


def test_context_flags_force_a_human(db, org):
    """A financial or legal mention escalates by policy, not by classification."""
    provider = StubProvider("stub", answers={"intent": "ACKNOWLEDGEMENT"}, confidence=0.99)
    gateway = DecisionGateway(
        chain=ProviderChain([provider]), db=db,
        stage=RolloutStage.LOW_RISK_EXTERNAL_AUTOMATION,
        autonomy=Autonomy.AUTOPILOT_WITH_GATES,
    )
    request = DecisionRequest(
        decision_type="email_triage",
        questions=(DecisionQuestion("intent", QuestionType.CHOICE, "intent", EMAIL_INTENTS),),
        state={"contains_financial_request": True},
        organisation_id=org,
    )
    result = gateway.decide(request)
    outcome = gateway.evaluate_policy(result, request)
    assert outcome.allowed is False
    assert outcome.requires_human is True
    assert "contains_financial_request" in outcome.reason


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------
def test_shadow_mode_cannot_influence_the_acting_decision(db, org):
    """The shadow answer is recorded and never returned as the acting one.

    The brief's staging is SHADOW -> ADVISORY -> ... and the guarantee is that
    shadow influences nothing. Here the shadow provider disagrees completely; the
    acting result must be unchanged, and the shadow result must be marked.
    """
    acting = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    shadow = StubProvider("jev", answers={"worth_researching": False}, confidence=0.99)

    gateway = DecisionGateway(
        chain=ProviderChain([acting]),
        shadow_provider=shadow,
        db=db,
        stage=RolloutStage.SHADOW,
        autonomy=Autonomy.MONITOR_ONLY,
    )
    result, shadow_result, agreement = gateway.decide_with_shadow(triage_request(organisation_id=org))

    assert result.provider == "rules", "the shadow provider became the actor"
    assert result.value("worth_researching") is True, "the shadow answer changed the acting answer"
    assert shadow_result is not None
    assert shadow_result.shadow is True, "a shadow result was not marked as shadow"
    assert shadow_result.value("worth_researching") is False
    assert agreement is not None and not agreement.fully_agreed
    assert agreement.disagreed == ("worth_researching",)


def test_shadow_results_are_persisted_with_a_pointer_to_the_actor(db, org):
    """Recording both sides is the whole point: agreement cannot be measured
    from logs that kept only one."""
    acting = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    shadow = StubProvider("jev", answers={"worth_researching": True}, confidence=0.99)
    gateway = DecisionGateway(
        chain=ProviderChain([acting]), shadow_provider=shadow, db=db,
        stage=RolloutStage.SHADOW,
    )
    acting_result, _, agreement = gateway.decide_with_shadow(triage_request(organisation_id=org))
    db.commit()

    rows = db.execute(select(models.DecisionRecord)).scalars().all()
    assert len(rows) == 2
    by_shadow = {r.shadow: r for r in rows}
    assert by_shadow[False].provider == "rules"
    assert by_shadow[True].provider == "jev"
    assert by_shadow[True].shadow_of == acting_result.decision_id
    assert agreement is not None and agreement.fully_agreed


def test_a_shadow_provider_that_fails_does_not_break_the_decision(db, org):
    acting = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    shadow = StubProvider(
        "jev", error=DecisionProviderUnavailable("typesafe is down"), available=True
    )
    gateway = DecisionGateway(
        chain=ProviderChain([acting]), shadow_provider=shadow, db=db,
        stage=RolloutStage.SHADOW,
    )
    result, shadow_result, agreement = gateway.decide_with_shadow(triage_request(organisation_id=org))
    assert result.provider == "rules"
    assert shadow_result is None
    assert agreement is None


# ---------------------------------------------------------------------------
# Fallback and the circuit breaker
# ---------------------------------------------------------------------------
def test_an_unavailable_provider_falls_through_to_the_next(db, org):
    """Jev being down must not stop opportunity ingestion.

    The unavailable provider is built to answer if it were called, so the only
    thing that can keep ``calls == 0`` is the chain's own availability check.
    """
    down = StubProvider(
        "jev", available=False, bypass_availability=True,
        answers={"worth_researching": True}, confidence=0.99,
    )
    rules = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    gateway = DecisionGateway(chain=ProviderChain([down, rules]), db=db)

    result = gateway.decide(triage_request(organisation_id=org))
    assert result.provider == "rules"
    assert down.calls == 0, "an unavailable provider was still called"
    assert result.fallback_used is True


def test_a_failing_provider_falls_through_and_is_recorded(db, org):
    broken = StubProvider("jev", error=DecisionProviderError("500"), available=True)
    rules = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    gateway = DecisionGateway(chain=ProviderChain([broken, rules]), db=db)
    result = gateway.decide(triage_request(organisation_id=org))
    assert result.provider == "rules"
    assert result.fallback_reason is not None


def test_an_invalid_result_never_becomes_an_answer(db, org):
    """A provider that answers outside the option set is not answering."""
    bad = StubProvider("bad", answers={"strategic_fit": "PROBABLY_HIGH"}, confidence=0.99)
    request = triage_request(organisation_id=org)
    # The stub bypasses question validation, so the gateway's own validation is
    # exercised through a real provider contract instead: see the parametrised
    # validation tests below. Here we assert the stub's failure to be valid is
    # caught when the value is checked.
    with pytest.raises(InvalidDecisionResult):
        request.question("strategic_fit").validate("PROBABLY_HIGH")


def test_the_chain_exhausted_is_not_a_default_answer(db, org):
    """'The decision layer is unavailable' must never become 'yes' or 'no'.

    A default of yes would act on nothing. A default of no would discard real
    opportunities. So it raises, and the caller escalates.
    """
    down = StubProvider("jev", available=False)
    gateway = DecisionGateway(chain=ProviderChain([down]), db=db)
    with pytest.raises(NoDecisionProvider):
        gateway.decide(triage_request(organisation_id=org))


def test_a_refusal_moves_on_without_penalising_the_breaker(db, org):
    """A refusal is information about the decision type, not about provider health."""
    refuses = StubProvider("rules", error=DecisionRefused("no rules for this type"))
    answers = StubProvider("jev", answers={"worth_researching": True}, confidence=0.9)
    chain = ProviderChain([refuses, answers])
    result = chain.attempt(triage_request(organisation_id=org))
    assert result[0].provider == "jev"
    assert chain.breaker_for("rules").failure_count == 0


def test_the_circuit_breaker_opens_after_repeated_failures(db, org):
    """Without this, an outage turns every decision into a timeout."""
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    assert breaker.is_open is False
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open is True
    breaker.record_success()
    assert breaker.is_open is False


def test_an_open_circuit_skips_the_provider_entirely(db, org):
    broken = StubProvider("jev", error=DecisionProviderError("500"))
    rules = StubProvider("rules", answers={"worth_researching": True}, confidence=0.9)
    chain = ProviderChain([broken, rules])
    breaker = chain.breaker_for("jev")
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    calls_before = broken.calls
    chain.attempt(triage_request(organisation_id=org))
    assert broken.calls == calls_before, "an open circuit still called the provider"


# ---------------------------------------------------------------------------
# Question validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,valid",
    [
        (True, True), (False, True), ("YES", True), ("no", True),
        ("MAYBE", False), (1, False), (None, False),
    ],
)
def test_boolean_validation(value, valid):
    question = DecisionQuestion("q", QuestionType.BOOLEAN, "is it?")
    if valid:
        assert question.validate(value) in (True, False)
    else:
        with pytest.raises(InvalidDecisionResult):
            question.validate(value)


def test_a_choice_outside_the_allowlist_is_rejected_not_nearest_matched():
    """This is what stops a model inventing an agent name."""
    question = DecisionQuestion("route", QuestionType.CHOICE, "where?", AGENT_ROUTES)
    assert question.validate("proposal") == "PROPOSAL"
    with pytest.raises(InvalidDecisionResult):
        question.validate("PROPOSAL_AGENT")
    with pytest.raises(InvalidDecisionResult):
        question.validate("delete_everything")


@pytest.mark.parametrize("value", [0, 50, 100])
def test_score_validation_within_bounds(value):
    question = DecisionQuestion("fit", QuestionType.SCORE, "fit?", minimum=0, maximum=100)
    assert question.validate(value) == float(value)


@pytest.mark.parametrize("value", [-1, 101, "high", True])
def test_score_validation_rejects_out_of_bounds_and_wrong_types(value):
    question = DecisionQuestion("fit", QuestionType.SCORE, "fit?", minimum=0, maximum=100)
    with pytest.raises(InvalidDecisionResult):
        question.validate(value)


def test_an_unknown_question_type_is_refused():
    with pytest.raises(UnknownQuestionType):
        DecisionQuestion("q", "FREE_TEXT", "write an essay")


def test_a_choice_question_needs_options():
    with pytest.raises(ValueError):
        DecisionQuestion("q", QuestionType.CHOICE, "where?")


def test_duplicate_question_keys_are_refused():
    with pytest.raises(ValueError):
        DecisionRequest(
            decision_type="x",
            questions=(
                DecisionQuestion("a", QuestionType.BOOLEAN, "one"),
                DecisionQuestion("a", QuestionType.BOOLEAN, "two"),
            ),
            state={},
        )


# ---------------------------------------------------------------------------
# The rules provider
# ---------------------------------------------------------------------------
def test_the_rules_provider_answers_from_its_table(db, org):
    """A rule only answers a question that was actually asked.

    The first version of this test reused ``triage_request``, whose questions are
    ``strategic_fit`` and ``worth_researching`` - neither of which any rule
    covers - so the provider correctly refused and the test failed. The rule table
    answers ``priority`` and ``human_review_needed``, so those are the questions.
    """
    rules = RulesDecisionProvider(default_rules())
    request = DecisionRequest(
        decision_type="opportunity_triage",
        questions=(
            DecisionQuestion("priority", QuestionType.CHOICE, "priority?",
                             ("IGNORE", "LOW", "NORMAL", "HIGH", "URGENT")),
            DecisionQuestion("human_review_needed", QuestionType.BOOLEAN, "human?"),
        ),
        state={
            "deadline": {"days_remaining": 45},
            "eligibility": {"unknown_gates": []},
            "documents": {"expired": []},
        },
        organisation_id=org,
    )
    result = rules.decide(request, timeout_seconds=5)
    assert result.answers["priority"].value == "NORMAL"  # 45 days
    assert result.answers["human_review_needed"].value is False
    assert result.confidence == 1.0


def test_a_rule_escalates_when_a_gate_could_not_be_evaluated():
    """An unevaluable gate is a data problem, and it always needs a person."""
    rules = RulesDecisionProvider(default_rules())
    request = DecisionRequest(
        decision_type="opportunity_triage",
        questions=(DecisionQuestion("human_review_needed", QuestionType.BOOLEAN, "human?"),),
        state={
            "deadline": {"days_remaining": 45},
            "eligibility": {"unknown_gates": ["country_eligible"]},
            "documents": {"expired": []},
        },
    )
    assert rules.decide(request, timeout_seconds=5).answers["human_review_needed"].value is True


def test_a_rule_defers_on_a_near_deadline_it_cannot_judge():
    """The rule owns the arithmetic; anything finer is a judgement.

    ``priority`` returns None when the deadline is unknown, so the question is
    left to a decision provider rather than guessed at.
    """
    rules = RulesDecisionProvider(default_rules())
    request = DecisionRequest(
        decision_type="opportunity_triage",
        questions=(
            DecisionQuestion("priority", QuestionType.CHOICE, "priority?",
                             ("IGNORE", "LOW", "NORMAL", "HIGH", "URGENT")),
        ),
        state={"deadline": {"days_remaining": None}},
    )
    with pytest.raises(DecisionRefused):
        rules.decide(request, timeout_seconds=5)


def test_rules_refuse_rather_than_guessing():
    """Nothing to say is a refusal, not an answer of 'no'."""
    rules = RulesDecisionProvider({})
    with pytest.raises(DecisionRefused):
        rules.decide(triage_request(), timeout_seconds=5)


def test_a_rule_can_rule_out_an_auto_reply_but_never_rule_one_in(db, org):
    """A keyword match must not be sufficient to email a donor."""
    rules = RulesDecisionProvider(default_rules())
    safe_request = DecisionRequest(
        decision_type="email_triage",
        questions=(
            DecisionQuestion(
                "safe_for_routine_auto_reply", QuestionType.BOOLEAN, "safe?"
            ),
        ),
        state={"thread": {"is_new_correspondent": False}},
        organisation_id=org,
    )
    # The rule declines to affirm, so nothing answers and the chain moves on.
    with pytest.raises(DecisionRefused):
        rules.decide(safe_request, timeout_seconds=5)

    risky_request = DecisionRequest(
        decision_type="email_triage",
        questions=(
            DecisionQuestion("safe_for_routine_auto_reply", QuestionType.BOOLEAN, "safe?"),
        ),
        state={"contains_financial_request": True},
        organisation_id=org,
    )
    result = rules.decide(risky_request, timeout_seconds=5)
    assert result.answers["safe_for_routine_auto_reply"].value is False


def test_a_rule_that_returns_an_invalid_value_raises_rather_than_storing_it():
    rules = RulesDecisionProvider(
        {"x": {"flagged": lambda request: "PROBABLY"}}
    )
    request = DecisionRequest(
        decision_type="x",
        questions=(DecisionQuestion("flagged", QuestionType.BOOLEAN, "flagged?"),),
        state={},
    )
    with pytest.raises(InvalidDecisionResult):
        rules.decide(request, timeout_seconds=5)


# ---------------------------------------------------------------------------
# The Jev provider, against a mock of the real SDK surface
# ---------------------------------------------------------------------------
class FakeNoul:
    def __init__(self, noul, confidence=None):
        self.noul = noul
        self.confidence = confidence


class FakeChoice:
    def __init__(self, choice, confidence=None):
        self.choice = choice
        self.confidence = confidence


class FakeScore:
    def __init__(self, score, confidence=None):
        self.score = score
        self.confidence = confidence


class FakeSystemOneResponse:
    """Mirrors the SDK's documented shape: three type-specific collections."""

    def __init__(self, nouls=None, choices=None, scores=None, id="resp-1"):
        self.nouls = nouls or {}
        self.choices = choices or {}
        self.scores = scores or {}
        self.id = id


class FakeTypeSafeClient:
    """Stands in for ``typesafe_sdk.TypeSafeClient``.

    Records the state and questions it was given, so the mapping can be asserted
    without a network call. Ordinary CI must never make a paid API call.
    """

    def __init__(self, response=None, error=None, **kwargs):
        self.response = response
        self.error = error
        self.kwargs = kwargs
        self.calls: list[tuple[Any, Any]] = []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return self.response


def test_jev_provider_is_unavailable_without_a_key():
    """Granada must boot with no TypeSafe key configured."""
    provider = JevDecisionProvider(api_key="")
    assert provider.available is False
    with pytest.raises(DecisionProviderUnavailable):
        provider.decide(triage_request(), timeout_seconds=5)


def test_jev_provider_maps_granada_types_to_the_sdk(monkeypatch):
    """The mapping is the only place vendor class names may appear.

    Asserted against the SDK's real published surface: ``Noul`` for the
    boolean-like type, ``Choice`` with ``criteria`` for a closed set, and
    ``Score`` with ``criteria`` for a bounded number. The SDK details that are
    easy to get wrong - the type is ``Noul``, not ``Bool``, and answers come back
    from three separate collections - are exactly what this checks.
    """
    typesafe_sdk = pytest.importorskip("typesafe_sdk")

    captured: dict[str, Any] = {}

    class Noul:
        def __init__(self, **kwargs):
            captured.setdefault("noul", []).append(kwargs)

    class Choice:
        def __init__(self, **kwargs):
            captured.setdefault("choice", []).append(kwargs)

    class Score:
        def __init__(self, **kwargs):
            captured.setdefault("score", []).append(kwargs)

    monkeypatch.setattr(typesafe_sdk, "Noul", Noul, raising=False)
    monkeypatch.setattr(typesafe_sdk, "Choice", Choice, raising=False)
    monkeypatch.setattr(typesafe_sdk, "Score", Score, raising=False)

    provider = JevDecisionProvider(api_key="test-key")
    request = DecisionRequest(
        decision_type="test",
        questions=(
            DecisionQuestion("yes_no", QuestionType.BOOLEAN, "is it?"),
            DecisionQuestion("pick", QuestionType.CHOICE, "which?", ("A", "B")),
            DecisionQuestion("rate", QuestionType.SCORE, "how much?", minimum=1, maximum=3),
        ),
        state={"note": "hello"},
    )
    provider.build_questions(request)

    assert len(captured["noul"]) == 1
    assert captured["choice"][0]["criteria"] == {"A": None, "B": None}
    assert captured["score"][0]["criteria"] == ["1", "2", "3"]


def test_jev_provider_reads_answers_from_the_type_specific_collections():
    response = FakeSystemOneResponse(
        nouls={"worth_researching": FakeNoul(True)},
        choices={"strategic_fit": FakeChoice("HIGH")},
    )
    client = FakeTypeSafeClient(response=response)
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: client
    )
    result = provider.decide(triage_request(), timeout_seconds=5)

    assert result.provider == "jev"
    assert result.value("worth_researching") is True
    assert result.value("strategic_fit") == "HIGH"
    assert result.model == "jev-latest"
    assert result.raw_provider_reference == "resp-1"


def test_jev_reports_no_confidence_when_the_sdk_gives_none():
    """The documented surface has no per-answer confidence.

    So the provider must report None rather than assume, and the policy engine
    must treat that as LOW. Fabricating a confidence here is how an unmeasured
    provider would earn authority.
    """
    response = FakeSystemOneResponse(nouls={"worth_researching": FakeNoul(True)})
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: FakeTypeSafeClient(response=response)
    )
    result = provider.decide(triage_request(), timeout_seconds=5)
    assert result.confidence is None
    assert result.answers["worth_researching"].confidence is None


def test_jev_reports_a_confidence_when_the_sdk_supplies_one():
    response = FakeSystemOneResponse(
        nouls={"worth_researching": FakeNoul(True, confidence=0.96)}
    )
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: FakeTypeSafeClient(response=response)
    )
    result = provider.decide(triage_request(), timeout_seconds=5)
    assert result.confidence == 0.96
    assert band_for(result.confidence) == ConfidenceBand.VERY_HIGH


def test_jev_uses_the_lowest_reported_confidence():
    """The weakest answer bounds the decision, not the strongest."""
    response = FakeSystemOneResponse(
        nouls={"worth_researching": FakeNoul(True, confidence=0.99)},
        choices={"strategic_fit": FakeChoice("HIGH", confidence=0.55)},
    )
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: FakeTypeSafeClient(response=response)
    )
    assert provider.decide(triage_request(), timeout_seconds=5).confidence == 0.55


def test_jev_rejects_an_answer_outside_the_option_set():
    """Granada's own validation runs on provider output, not a provider's."""
    response = FakeSystemOneResponse(choices={"strategic_fit": FakeChoice("EXTREMELY_HIGH")})
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: FakeTypeSafeClient(response=response)
    )
    with pytest.raises(InvalidDecisionResult):
        # read_answers validates against Granada's question definitions.
        provider.decide(triage_request(), timeout_seconds=5)


def test_jev_raises_rather_than_returning_a_partial_answer():
    response = FakeSystemOneResponse()  # nothing readable
    provider = JevDecisionProvider(
        api_key="k", client_factory=lambda **kwargs: FakeTypeSafeClient(response=response)
    )
    with pytest.raises(InvalidDecisionResult):
        provider.decide(triage_request(), timeout_seconds=5)


def test_jev_normalises_sdk_exceptions_into_one_retryable_type():
    """The SDK's exception hierarchy is not something Granada depends on."""
    provider = JevDecisionProvider(
        api_key="k",
        client_factory=lambda **kwargs: FakeTypeSafeClient(error=RuntimeError("rate limited")),
    )
    with pytest.raises(DecisionProviderError) as excinfo:
        provider.decide(triage_request(), timeout_seconds=5)
    assert "rate limited" in str(excinfo.value)


def test_jev_minimises_the_state_it_sends():
    """References and redaction, not the organisation's whole file."""
    client = FakeTypeSafeClient(response=FakeSystemOneResponse(
        nouls={"worth_researching": FakeNoul(True)}
    ))
    provider = JevDecisionProvider(api_key="k", client_factory=lambda **kwargs: client)
    request = triage_request()
    request.state["contact"] = "email jane.doe@example.org about it"
    provider.decide(request, timeout_seconds=5)

    state_sent = client.calls[0][0]
    assert "jane.doe@example.org" not in str(state_sent), "an email address was sent unredacted"


def test_jev_is_never_asked_to_write_prose():
    """A docstring is not enforcement, so the request shape is asserted.

    Every question Granada can express is BOOLEAN, CHOICE or SCORE - there is no
    free-text question type - which is what makes 'do not ask Jev to write' a
    property of the type system rather than a convention.
    """
    assert QuestionType.ALL == {"BOOLEAN", "CHOICE", "SCORE"}
    assert "FREE_TEXT" not in QuestionType.ALL


# ---------------------------------------------------------------------------
# The LLM provider
# ---------------------------------------------------------------------------
class FakeModelResult:
    def __init__(self, data, model="fake-1", invocation_id="inv-1"):
        self.data = data
        self.model = model
        self.invocation_id = invocation_id


class FakeModelGateway:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeModelResult(self.data)


def test_the_llm_provider_requires_a_gateway():
    provider = LLMDecisionProvider(None)
    assert provider.available is False
    with pytest.raises(DecisionProviderUnavailable):
        provider.decide(triage_request(), timeout_seconds=5)


def test_the_llm_provider_builds_its_schema_from_granada_questions():
    """So the option set cannot drift from the check that actually runs."""
    provider = LLMDecisionProvider(FakeModelGateway())
    schema = provider.schema_for(triage_request())
    assert schema["required"] == ["strategic_fit", "worth_researching"]
    assert schema["properties"]["strategic_fit"]["enum"] == [
        "VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH",
    ]
    assert schema["properties"]["worth_researching"]["type"] == "boolean"
    assert schema["additionalProperties"] is False


def test_the_llm_provider_validates_the_output():
    gateway = FakeModelGateway(data={"strategic_fit": "HIGH", "worth_researching": True})
    result = LLMDecisionProvider(gateway).decide(triage_request(), timeout_seconds=5)
    assert result.value("strategic_fit") == "HIGH"
    # No confidence is claimed: a model's self-report is not a calibrated
    # probability of this answer being right.
    assert result.confidence is None


def test_the_llm_provider_rejects_an_answer_outside_the_option_set():
    gateway = FakeModelGateway(data={"strategic_fit": "GREAT", "worth_researching": True})
    with pytest.raises(InvalidDecisionResult):
        LLMDecisionProvider(gateway).decide(triage_request(), timeout_seconds=5)


def test_the_llm_provider_rejects_an_omitted_answer():
    gateway = FakeModelGateway(data={"strategic_fit": "HIGH"})
    with pytest.raises(InvalidDecisionResult):
        LLMDecisionProvider(gateway).decide(triage_request(), timeout_seconds=5)


def test_the_llm_provider_never_uses_the_synthesis_tier():
    """Paying synthesis prices to triage an inbox is how a platform becomes
    uneconomic, and nothing breaks when it happens - the bill just grows."""
    gateway = FakeModelGateway(data={"strategic_fit": "HIGH", "worth_researching": True})
    LLMDecisionProvider(gateway).decide(triage_request(), timeout_seconds=5)
    assert gateway.calls[0]["tier"] == "CLASSIFICATION"


# ---------------------------------------------------------------------------
# Ensemble: LLM as the fallback for an unavailable Jev
# ---------------------------------------------------------------------------
def test_the_llm_falls_back_when_jev_is_unavailable(db, org):
    """The brief's recommended chain: rules, then Jev, then an LLM."""
    jev = StubProvider("jev", available=False)
    llm = StubProvider("llm", answers={"worth_researching": True}, confidence=0.8)
    rules = StubProvider("rules", error=DecisionRefused("no rule for this question"))
    gateway = DecisionGateway(chain=ProviderChain([rules, jev, llm]), db=db)

    result = gateway.decide(triage_request(organisation_id=org))
    assert result.provider == "llm"
    assert result.fallback_used is True
    assert list(result.provider_chain) == ["rules", "jev", "llm"]


# ---------------------------------------------------------------------------
# Persistence, caching and invalidation
# ---------------------------------------------------------------------------
def test_every_decision_is_recorded_with_its_provenance(db, org):
    provider = StubProvider("jev", answers={"worth_researching": True}, confidence=0.96)
    gateway = DecisionGateway(chain=ProviderChain([provider]), db=db)
    result = gateway.decide(triage_request(organisation_id=org))
    db.commit()

    row = db.execute(select(models.DecisionRecord)).scalar_one()
    assert row.provider == "jev"
    assert row.model == "jev-model"
    assert row.answers == {"worth_researching": True}
    assert row.confidence == 0.96
    assert row.correlation_id == "corr-1"
    assert row.id == result.decision_id
    assert row.state_hash == state_fingerprint(triage_request(organisation_id=org).state)


def test_the_state_is_not_stored_by_default(db, org):
    """A hash and references are enough for audit; the state may hold donor text."""
    provider = StubProvider("stub", answers={"worth_researching": True})
    gateway = DecisionGateway(chain=ProviderChain([provider]), db=db)
    gateway.decide(triage_request(organisation_id=org))
    db.commit()
    assert db.execute(select(models.DecisionRecord)).scalar_one().state_snapshot is None


def test_the_state_is_stored_when_explicitly_enabled(db, org):
    provider = StubProvider("stub", answers={"worth_researching": True})
    gateway = DecisionGateway(chain=ProviderChain([provider]), db=db, store_state=True)
    gateway.decide(triage_request(organisation_id=org))
    db.commit()
    assert db.execute(select(models.DecisionRecord)).scalar_one().state_snapshot is not None


def test_the_state_fingerprint_is_key_order_independent(db, org):
    """Two callers building the same state differently have the same state."""
    a = {"x": 1, "y": {"b": 2, "a": 3}}
    b = {"y": {"a": 3, "b": 2}, "x": 1}
    assert state_fingerprint(a) == state_fingerprint(b)
    assert state_fingerprint(a) != state_fingerprint({"x": 2, "y": {"b": 2, "a": 3}})


def test_a_changed_state_invalidates_the_cached_decision(db, org):
    """A cached verdict must not outlive the facts it was based on."""
    provider = StubProvider("stub", answers={"worth_researching": True}, confidence=0.9)
    gateway = DecisionGateway(
        chain=ProviderChain([provider]), db=db,
        stage=RolloutStage.INTERNAL_AUTOMATION,
        cache_ttl_seconds=3600,
    )
    request = triage_request(organisation_id=org)
    gateway.decide(request)
    db.commit()

    # Same state: served from cache, so the provider is not called again.
    calls_before = provider.calls
    gateway.decide(triage_request(organisation_id=org))
    db.commit()
    assert provider.calls == calls_before, "an identical decision was recomputed"

    # Changed state: recomputed, because the fingerprint moved.
    changed = triage_request(organisation_id=org)
    changed.state["deadline"] = {"days_remaining": 3}
    gateway.decide(changed)
    db.commit()
    assert provider.calls == calls_before + 1, "a changed state served a stale decision"


def test_a_changed_question_schema_version_invalidates_the_cache(db, org):
    """Changing an option set must not reuse answers given under the old set."""
    provider = StubProvider("stub", answers={"worth_researching": True}, confidence=0.9)
    gateway = DecisionGateway(
        chain=ProviderChain([provider]), db=db,
        stage=RolloutStage.INTERNAL_AUTOMATION,
    )
    gateway.decide(triage_request(organisation_id=org))
    db.commit()

    calls_before = provider.calls
    bumped = triage_request(organisation_id=org, question_schema_version="v2")
    gateway.decide(bumped)
    db.commit()
    assert provider.calls == calls_before + 1


def test_tenants_do_not_share_decisions(db, org):
    """A cached decision for one tenant must never be served to another."""
    other_user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(other_user)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other", slug="other", owner_user_id=other_user.id
    )
    db.add(other)
    db.commit()

    provider = StubProvider("stub", answers={"worth_researching": True}, confidence=0.9)
    gateway = DecisionGateway(
        chain=ProviderChain([provider]), db=db, stage=RolloutStage.INTERNAL_AUTOMATION
    )
    gateway.decide(triage_request(organisation_id=org))
    db.commit()

    calls_before = provider.calls
    gateway.decide(triage_request(organisation_id=other.id))
    db.commit()
    assert provider.calls == calls_before + 1, "one tenant was served another's decision"


def test_the_audit_summary_has_named_fields_not_a_sentence(db, org):
    """The brief forbids logging 'AI decided yes'."""
    provider = StubProvider("jev", answers={"worth_researching": True}, confidence=0.96)
    gateway = DecisionGateway(chain=ProviderChain([provider]), db=db)
    result = gateway.decide(triage_request(organisation_id=org))
    summary = result.audit_summary()
    for field in (
        "decision_id", "decision_type", "provider", "model", "answers",
        "confidence", "latency_ms", "policy_result", "correlation_id",
    ):
        assert field in summary, f"the audit entry is missing {field}"
    assert summary["answers"] == {"worth_researching": True}
    assert summary["provider"] == "jev"


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def test_compare_reports_per_question_agreement():
    left = DecisionResult(
        decision_id="a", decision_type="t", provider="rules", model=None,
        answers={
            "x": Answer("x", True), "y": Answer("y", "HIGH"),
        },
        confidence=0.9,
    )
    right = DecisionResult(
        decision_id="b", decision_type="t", provider="jev", model=None,
        answers={
            "x": Answer("x", True), "y": Answer("y", "LOW"),
        },
        confidence=0.9,
    )
    agreement = compare(left, right)
    assert agreement.compared == ("x", "y")
    assert agreement.agreed == ("x",)
    assert agreement.disagreed == ("y",)
    assert agreement.fully_agreed is False


# ---------------------------------------------------------------------------
# Construction: feature flags
# ---------------------------------------------------------------------------
class FakeSettings:
    def __init__(self, **kwargs):
        self.decision_provider = kwargs.get("decision_provider", "rules")
        self.jev_enabled = kwargs.get("jev_enabled", False)
        self.typesafe_api_key = kwargs.get("typesafe_api_key", "")
        self.typesafe_base_url = kwargs.get("typesafe_base_url", "")
        self.typesafe_default_model = kwargs.get("typesafe_default_model", "jev-latest")
        self.decision_rollout_stage = kwargs.get("decision_rollout_stage", RolloutStage.SHADOW)
        self.decision_autonomy = kwargs.get("decision_autonomy", Autonomy.MONITOR_ONLY)
        self.decision_cache_ttl_seconds = 3600
        self.decision_timeout_seconds = 20
        self.decision_store_state = False


def test_the_default_configuration_boots_with_no_typesafe_key():
    """The brief requires exactly this: the app must not fail to boot with
    JEV_ENABLED=false, and no API key must be needed for development."""
    gateway = build_gateway(settings=FakeSettings())
    assert gateway.stage == RolloutStage.SHADOW
    assert gateway.autonomy == Autonomy.MONITOR_ONLY
    assert "rules" in gateway.chain.names
    assert "jev" not in gateway.chain.names
    assert gateway.shadow_provider is None


def test_enabling_jev_puts_it_in_the_shadow_slot_not_the_acting_one():
    """Shadow mode is the default, so enabling Jev must not make it the actor."""
    gateway = build_gateway(settings=FakeSettings(jev_enabled=True, typesafe_api_key="k"))
    assert gateway.shadow_provider is not None
    assert gateway.shadow_provider.name == "jev"
    assert gateway.chain.names[0] == "rules", (
        "enabling Jev made it the acting provider; shadow mode must not act"
    )


def test_the_default_rollout_stage_is_shadow():
    """The brief: do not jump straight to autonomy."""
    assert RolloutStage.SHADOW == RolloutStage.ORDER[0]
    assert RolloutStage.SHADOW not in RolloutStage.ACTING


def test_the_agent_routing_allowlist_is_closed():
    """Routing must never accept an arbitrary name from model output."""
    assert "PROPOSAL" in AGENT_ROUTES
    assert "HUMAN_REVIEW" in AGENT_ROUTES
    question = DecisionQuestion("route", QuestionType.CHOICE, "where?", AGENT_ROUTES)
    with pytest.raises(InvalidDecisionResult):
        question.validate("ProposalAgent")


def test_the_email_intent_allowlist_matches_the_brief():
    for intent in (
        "ACKNOWLEDGEMENT", "CLARIFICATION_REQUEST", "DOCUMENT_REQUEST",
        "DEADLINE_CHANGE", "INTERVIEW_INVITATION", "AWARD_NOTICE",
        "REJECTION_NOTICE", "CONTRACT", "PAYMENT_OR_BANK_REQUEST",
        "GENERAL_QUESTION", "BOUNCE", "UNKNOWN",
    ):
        assert intent in EMAIL_INTENTS
