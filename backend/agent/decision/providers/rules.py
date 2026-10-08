"""The deterministic rules provider.

This is the baseline every other provider is measured against, and it is the
reason the fallback story is credible: when no other provider can answer, Granada does not
stop - it falls back to rules that were already answering these questions.

It is also the honest control group. An evaluation that compares a model against
nothing proves nothing; the brief requires comparing a model against a deterministic
baseline and an LLM, and this is the deterministic one.

Everything here is a pure function of the request. No network, no model, no
time-dependence beyond what the caller passed in - so a cached or replayed result
is identical, and an unexpected answer is a bug rather than a mood.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from agent.decision.models import Answer, DecisionRequest, DecisionResult, QuestionType
from agent.decision.exceptions import DecisionRefused
from agent.decision.providers.base import BaseDecisionProvider

#: A rule maps a request to a value for one question key, or to ``None`` when it
#: has no opinion. ``None`` is not an answer: the gateway treats the question as
#: unanswerable by this provider.
Rule = Callable[[DecisionRequest], Any]


class RulesDecisionProvider(BaseDecisionProvider):
    """Answers from an explicit table of deterministic rules.

    Deterministic answers carry confidence ``1.0`` *about the rule*, not about
    the world. That distinction is recorded in the reason strings rather than
    being implied by the number: a rule that says "deadline passed" is certain
    that the deadline passed, and that is all it is certain of.
    """

    name = "rules"

    def __init__(self, rules: Optional[dict[str, dict[str, Rule]]] = None) -> None:
        #: decision_type -> {question_key: rule}
        self.rules: dict[str, dict[str, Rule]] = rules or {}
        self.available = True

    def register(self, decision_type: str, question_key: str, rule: Rule) -> None:
        self.rules.setdefault(decision_type, {})[question_key] = rule

    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        table = self.rules.get(request.decision_type, {})
        answers: dict[str, Answer] = {}
        unanswered: list[str] = []

        for question in request.questions:
            rule = table.get(question.key)
            if rule is None:
                unanswered.append(question.key)
                continue
            value = rule(request)
            if value is None:
                unanswered.append(question.key)
                continue
            # A rule that returns something the question does not accept is a
            # bug in the rule, and it must fail here rather than be recorded.
            validated = question.validate(value)
            answers[question.key] = Answer(
                key=question.key, value=validated, confidence=1.0
            )

        if not answers:
            # Nothing to say is a refusal, not an answer of "no". The gateway
            # will move to the next provider.
            raise DecisionRefused(
                f"no rules registered for {request.decision_type!r} covering "
                f"{[q.key for q in request.questions]}; unanswered={unanswered}"
            )

        return DecisionResult(
            decision_id=request.decision_id,
            decision_type=request.decision_type,
            provider=self.name,
            model=None,
            answers=answers,
            confidence=1.0,
            latency_ms=0,
            correlation_id=request.correlation_id,
            question_schema_version=request.question_schema_version,
            provider_chain=(self.name,),
        )


# ---------------------------------------------------------------------------
# Ready-made rules for Granada's decision types
# ---------------------------------------------------------------------------
def triage_rules() -> dict[str, Rule]:
    """Deterministic rules for opportunity triage.

    These encode only things Granada already knows for certain from its own
    records - a deadline comparison, whether a document is approved. They
    deliberately do **not** judge fit; that is what a decision provider is for,
    and a rule that pretended to judge fit would be a model with no model in it.
    """

    def worth_researching(request: DecisionRequest) -> Any:
        """A deterministic proxy for the judgment, and nothing more.

        It answers only from facts Granada holds: if a hard gate failed, no; if
        every gate passed and none is unknown, yes. Anything in between refuses, so
        the answer is a function of the deterministic result rather than a guess -
        the judgmental part ("is this a *good* fit") needs a provider that can
        actually judge, and the chain moves on when this one will not answer.

        This replaces the questions the qualify step asks being unanswerable by the
        deterministic baseline, which parked every workflow on the first sweep and
        meant the fleet never completed a pipeline.
        """
        eligibility = request.state.get("eligibility", {})
        if eligibility.get("failed_gates"):
            return False
        if eligibility.get("unknown_gates"):
            return None
        return True

    def human_review_needed(request: DecisionRequest) -> Any:
        """Certain cases only.

        A missing country or an unknown registration is a data problem, not a
        judgement, and it always needs a person.
        """
        gates = request.state.get("eligibility", {}).get("unknown_gates") or []
        if gates:
            return True
        if request.state.get("documents", {}).get("expired"):
            return True
        return False

    def priority(request: DecisionRequest) -> Any:
        """A deadline-proximity band, nothing more.

        Named ``priority`` because that is the question, but the rule only owns
        the part of it that is arithmetic. Anything finer is a judgement and
        belongs to a decision provider.
        """
        days = request.state.get("deadline", {}).get("days_remaining")
        if days is None:
            return None
        if days <= 7:
            return "URGENT"
        if days <= 21:
            return "HIGH"
        if days <= 60:
            return "NORMAL"
        return "LOW"

    return {
        "human_review_needed": human_review_needed,
        "priority": priority,
        "worth_researching": worth_researching,
    }


def email_rules() -> dict[str, Rule]:
    """Deterministic rules for email triage.

    The two that matter are the safety ones. A message that mentions a bank
    account change or a contract is escalated to a human by rule, before any
    model is consulted, because those are the two categories where a
    misclassification is expensive and irreversible.
    """

    def requires_human(request: DecisionRequest) -> Any:
        if request.state.get("contains_financial_request"):
            return True
        if request.state.get("contains_legal_commitment"):
            return True
        if request.state.get("thread", {}).get("is_new_correspondent"):
            # A brand-new sender asking for anything is worth a look.
            return True
        return False

    def safe_for_routine_auto_reply(request: DecisionRequest) -> Any:
        """Only ever ``False`` from a rule.

        A rule can rule an auto-reply *out* - if money, a contract, or an
        unrecognised sender is involved - but it must never rule one *in*.
        Permitting an outbound message on a keyword match alone is how a
        confident wrong reply reaches a donor. The affirmative case requires a
        decision provider and then Granada's policy.
        """
        if requires_human(request):
            return False
        return None

    return {"requires_human": requires_human, "safe_for_routine_auto_reply": safe_for_routine_auto_reply}


def readiness_rules() -> dict[str, Rule]:
    """Deterministic rules for application readiness.

    These are the checks that are simply true or false, and the brief is explicit
    that a decision model must never override them: a missing mandatory document
    cannot be reasoned away.
    """

    def needs_human_review(request: DecisionRequest) -> Any:
        readiness = request.state.get("readiness", {})
        if readiness.get("missing_required_questions"):
            return True
        if readiness.get("missing_documents"):
            return True
        if readiness.get("expired_documents"):
            return True
        if readiness.get("budget_invalid"):
            return True
        return False

    return {"needs_human_review": needs_human_review}


def routing_rules() -> dict[str, Rule]:
    """Deterministic routing for the cases that need no judgement at all.

    Everything else routes to ``HUMAN_REVIEW`` if no provider can decide, and
    that default is deliberate: an unroutable event must not silently become
    ``NO_ACTION``, because "we didn't know what to do" and "there is nothing to
    do" look identical in a log and are not the same thing.
    """

    def route(request: DecisionRequest) -> Any:
        event = request.state.get("event", {})
        if event.get("security_flagged"):
            return "HUMAN_REVIEW"
        if event.get("hard_rule_failed"):
            return "NO_ACTION"
        return None

    return {"route": route}


def default_rules() -> dict[str, dict[str, Rule]]:
    return {
        "opportunity_triage": triage_rules(),
        "email_triage": email_rules(),
        "application_readiness": readiness_rules(),
        "agent_routing": routing_rules(),
    }
