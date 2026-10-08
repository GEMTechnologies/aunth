"""Granada's own decision engine: standalone, explainable, and it refuses to guess.

The provider this replaced needed an account with an external service and an API key to answer
questions about an organisation's own data. For a self-hosted product that is the wrong shape: it
makes deployment depend on a vendor being reachable and puts an NGO's eligibility data through a
third party to answer something Granada can answer itself.

These tests pin the four properties that make the replacement trustworthy rather than merely
present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.decision.exceptions import DecisionProviderUnavailable  # noqa: E402
from agent.decision.models import (  # noqa: E402
    DecisionQuestion,
    DecisionRequest,
    QuestionType,
)
from agent.decision.providers.local import (  # noqa: E402
    LocalDecisionProvider,
    Signal,
    derive_signals,
)

BOOLEAN = DecisionQuestion(key="worth_pursuing", type=QuestionType.BOOLEAN, instructions="?")


def request_for(question: DecisionQuestion, **state) -> DecisionRequest:
    return DecisionRequest(
        decision_type="opportunity_triage", questions=(question,), state=state
    )


# ===========================================================================
# IT IS STANDALONE
# ===========================================================================
def test_the_provider_is_always_available_and_has_no_dependency_to_be_missing():
    """`available` False means the chain skips it. This one cannot be unavailable.

    It reads dicts. There is no key to configure, no network to reach, and no package that can be
    missing at runtime.
    """
    provider = LocalDecisionProvider()
    assert provider.available is True
    assert provider.name == "local"


def test_the_default_chain_builds_and_contains_no_vendor():
    """THE brief's requirement: a provider-neutral gateway, and nothing to configure.

    It must build with default settings, offline, with no key set anywhere.
    """
    from agent.decision.gateway import build_gateway
    from config import settings

    gateway = build_gateway(settings=settings)
    names = list(gateway.chain.names)
    assert "rules" in names, "the deterministic gates must come first"
    assert "local" in names, "Granada's own engine is missing from the default chain"
    assert not hasattr(settings, "typesafe_api_key"), (
        "a vendor key is still a setting, so a deployment can still be configured to depend on one"
    )
    assert not hasattr(settings, "jev_enabled")


def test_the_provider_module_has_no_vendor_import():
    """A vendor import is how the dependency comes back. Asserted on the source, not on behaviour,
    because an unused import still means a package that must be installed."""
    source = (BACKEND / "agent" / "decision" / "providers" / "local.py").read_text(
        encoding="utf-8"
    )
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            lowered = stripped.lower()
            assert "typesafe" not in lowered and "jev" not in lowered, (
                f"the local engine imports a vendor: {stripped}"
            )


# ===========================================================================
# IT EXPLAINS ITSELF
# ===========================================================================
def test_every_answer_carries_the_evidence_that_produced_it():
    """The brief requires a "Why?" view with evidence.

    It is satisfied by the same data that produced the answer, not by an explanation written
    afterwards - an explanation written afterwards is a story, not evidence.
    """
    provider = LocalDecisionProvider()
    result = provider.decide(
        request_for(BOOLEAN, opportunity={"eligible": True, "days_to_deadline": 30},
                    match={"fit_score": 0.8}),
        timeout_seconds=5,
    )

    evidence = result.policy_result["evidence"]
    assert evidence, "an answer with no recorded evidence cannot be reviewed"
    for entry in evidence:
        assert entry["source"], "a signal that cannot say where it came from is not evidence"
        assert entry["detail"], "a signal with no detail gives the reviewer nothing to read"

    sources = {entry["source"] for entry in evidence}
    assert "opportunity.eligible" in sources
    assert "opportunity.days_to_deadline" in sources


def test_the_evidence_names_the_reason_not_just_the_value():
    """A reviewer needs the sentence, not the boolean."""
    provider = LocalDecisionProvider()
    result = provider.decide(
        request_for(BOOLEAN, opportunity={"eligible": False}), timeout_seconds=5
    )
    detail = result.policy_result["evidence"][0]["detail"]
    assert "does not meet" in detail, detail


# ===========================================================================
# IT REFUSES TO GUESS
# ===========================================================================
def test_a_question_with_no_evidence_is_declined_rather_than_answered():
    """THE property the gateway's fallback depends on.

    The provider contract says implementations must raise rather than return a guessed answer,
    because the gateway keys its fallback on the exception. A guess silently removes Granada's
    ability to tell "answered" from "failed" - and an eligibility decision that was never made
    would look exactly like one that was.
    """
    provider = LocalDecisionProvider()
    with pytest.raises(DecisionProviderUnavailable):
        provider.decide(
            request_for(
                DecisionQuestion(key="something_nothing_knows", type=QuestionType.BOOLEAN,
                                 instructions="?"),
                opportunity={"eligible": True},
            ),
            timeout_seconds=5,
        )


def test_an_empty_state_is_declined():
    provider = LocalDecisionProvider()
    with pytest.raises(DecisionProviderUnavailable):
        provider.decide(request_for(BOOLEAN), timeout_seconds=5)


def test_a_choice_outside_the_allowlist_is_refused():
    """The question's options are an allowlist, not a hint.

    Accepting a value outside it would let the engine invent a route or an email intent that
    nothing downstream handles - which fails silently rather than loudly.
    """
    question = DecisionQuestion(
        key="route", type=QuestionType.CHOICE, instructions="?", options=("MATCHING", "EMAIL")
    )
    provider = LocalDecisionProvider()
    with pytest.raises(DecisionProviderUnavailable):
        provider.decide(
            DecisionRequest(
                decision_type="agent_routing",
                questions=(question,),
                state={"event": {"suggested_route": "NOT_AN_ALLOWED_OPTION"}},
            ),
            timeout_seconds=5,
        )


# ===========================================================================
# IT DOES NOT FABRICATE CONFIDENCE
# ===========================================================================
def test_confidence_is_computed_from_coverage_and_margin_not_assumed():
    """A single weak signal must NOT produce a confident answer.

    The previous provider's docstring made this point and it applies at least as much here: an
    unearned high confidence would let this engine qualify for autonomy it has not demonstrated.
    """
    provider = LocalDecisionProvider()
    weak = provider.decide(
        DecisionRequest(
            decision_type="opportunity_triage",
            questions=(BOOLEAN,),
            state={"signals": [{"key": "worth_pursuing", "value": True, "weight": 0.05,
                                "source": "test", "detail": "one faint signal"}]},
        ),
        timeout_seconds=5,
    )
    confidence = weak.answers["worth_pursuing"].confidence
    assert confidence is not None
    assert confidence < 0.2, (
        f"one weak signal produced confidence {confidence}; coverage and margin must both "
        "constrain it"
    )


def test_strong_agreeing_evidence_produces_high_confidence():
    """The counterpart, so the previous test is not satisfied by always reporting zero."""
    provider = LocalDecisionProvider()
    strong = provider.decide(
        DecisionRequest(
            decision_type="opportunity_triage",
            questions=(BOOLEAN,),
            state={
                "signals": [
                    {"key": "worth_pursuing", "value": True, "weight": 2.0,
                     "source": "a", "detail": "hard gate passed"},
                    {"key": "worth_pursuing", "value": True, "weight": 1.5,
                     "source": "b", "detail": "fit is strong"},
                    {"key": "worth_pursuing", "value": True, "weight": 1.0,
                     "source": "c", "detail": "deadline is comfortable"},
                ]
            },
        ),
        timeout_seconds=5,
    )
    confidence = strong.answers["worth_pursuing"].confidence
    assert confidence > 0.8, f"three agreeing signals produced only {confidence}"


def test_disagreeing_evidence_lowers_confidence():
    """Plenty of evidence that conflicts is not confidence, and must not read as any."""
    provider = LocalDecisionProvider()
    conflicting = provider.decide(
        DecisionRequest(
            decision_type="opportunity_triage",
            questions=(BOOLEAN,),
            state={
                "signals": [
                    {"key": "worth_pursuing", "value": True, "weight": 1.0,
                     "source": "a", "detail": "eligible"},
                    {"key": "worth_pursuing", "value": False, "weight": 0.95,
                     "source": "b", "detail": "deadline almost gone"},
                ]
            },
        ),
        timeout_seconds=5,
    )
    confidence = conflicting.answers["worth_pursuing"].confidence
    assert confidence < 0.1, (
        f"near-tied evidence produced {confidence}; the margin must constrain confidence as much "
        "as the coverage does"
    )


# ===========================================================================
# IT IS DETERMINISTIC
# ===========================================================================
def test_the_same_input_produces_the_same_answer():
    """No sampling, no clock, no network. Two identical decisions cannot disagree.

    That is what makes an evaluation dataset meaningful and what makes a recorded decision
    reproducible by whoever reviews it.
    """
    state = {
        "opportunity": {"eligible": True, "days_to_deadline": 21},
        "match": {"fit_score": 0.7},
    }
    provider = LocalDecisionProvider()
    first = provider.decide(request_for(BOOLEAN, **state), timeout_seconds=5)
    second = provider.decide(request_for(BOOLEAN, **state), timeout_seconds=5)
    assert first.answers == second.answers
    assert first.confidence == second.confidence


# ===========================================================================
# THE EXTRACTORS
# ===========================================================================
@pytest.mark.parametrize(
    "decision_type,state,expected",
    [
        ("opportunity_triage", {"opportunity": {"eligible": True}}, "opportunity.eligible"),
        ("email_triage", {"message": {"classification": "AWARD_NOTICE"}}, "message.classification"),
        ("application_readiness", {"application": {"blocking_conditions": []}},
         "application.blocking_conditions"),
        ("agent_routing", {"event": {"suggested_route": "MATCHING"}}, "event.suggested_route"),
    ],
)
def test_each_decision_type_derives_its_declared_signals(decision_type, state, expected):
    signals = derive_signals(decision_type, state)
    assert expected in {signal.source for signal in signals}


def test_an_unknown_decision_type_produces_no_signals():
    """Which is what makes the provider decline rather than invent an answer."""
    assert derive_signals("a_decision_type_nobody_declared", {"anything": True}) == []


def test_a_caller_may_supply_its_own_signals():
    """How a specialist signals something the generic extractors do not know about, without this
    module having to anticipate it."""
    signals = derive_signals(
        "opportunity_triage",
        {"signals": [{"key": "worth_pursuing", "value": True, "weight": 2.0,
                      "source": "specialist", "detail": "the programme officer has visited"}]},
    )
    assert any(signal.source == "specialist" for signal in signals)


def test_malformed_supplied_signals_are_skipped_not_crashed_on():
    """A caller's typo must not take out the decision path."""
    signals = derive_signals(
        "opportunity_triage",
        {"signals": ["not a mapping", {"no_key": 1}, {"key": "ok", "value": True}]},
    )
    assert [s.key for s in signals] == ["ok"]


def test_partial_evidence_answers_what_it_can_and_reports_the_rest():
    """Declining entirely would be as unhelpful as guessing.

    A question with evidence is answered; a question without is named in `fallback_reason` so the
    reviewer knows what was not decided.
    """
    provider = LocalDecisionProvider()
    result = provider.decide(
        DecisionRequest(
            decision_type="opportunity_triage",
            questions=(
                BOOLEAN,
                DecisionQuestion(key="unknown_one", type=QuestionType.BOOLEAN, instructions="?"),
            ),
            state={"opportunity": {"eligible": True}},
        ),
        timeout_seconds=5,
    )
    assert "worth_pursuing" in result.answers
    assert "unknown_one" not in result.answers
    assert "unknown_one" in (result.fallback_reason or "")
