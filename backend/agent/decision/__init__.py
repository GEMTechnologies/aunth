"""Decision Gateway: provider-neutral bounded decisions.

What this is
------------
A seam. Granada needs some decisions that are *narrow, structured and fast* -
is this organisation eligible, what kind of donor email is this, does this
require human approval, which agent should handle this event. Those are not
writing tasks, and using a long-form model for them is both slower and less
reliable than asking a purpose-built decision model a typed question.

The local engine is one provider behind that seam. It is **not** the
proposal writer, the researcher, the email writer, or the grant-writing model.
Prose, synthesis and long-form reasoning stay with ordinary LLMs.

The rule the brief is emphatic about
------------------------------------
**The code owns permission. The decision model does not.**

```
decision  ->  DecisionResult
          ->  PolicyEngine        (Granada owns this)
          ->  ApprovalEngine      (Granada owns this)
          ->  WorkflowEngine
          ->  Executor            (the only thing with side effects)
```

There is no path from a provider to a side effect. A provider cannot send an
email, submit an application, or move money; it returns answers, and everything
after that is Granada's code deciding with a confidence threshold, an autonomy
level and a policy.

Shadow mode
-----------
``SHADOW`` runs the decision **without letting it influence anything**. Granada
does whatever it did before, the provider's answer is recorded alongside, and the
two are compared. This is the only honest way to know whether a decision provider
is any good *for Granada's* cases, as opposed to on a published benchmark, and it
is why the default is ``SHADOW`` rather than anything that acts.
"""

from __future__ import annotations

from agent.decision.exceptions import (
    DecisionError,
    DecisionProviderError,
    DecisionProviderUnavailable,
    DecisionRefused,
    InvalidDecisionResult,
    NoDecisionProvider,
    PolicyBlocked,
    UnknownQuestionType,
)
from agent.decision.gateway import DecisionGateway, ProviderChain, build_gateway
from agent.decision.models import (
    Answer,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
    QuestionType,
)
from agent.decision.policy import (
    CONFIDENCE_BANDS,
    Autonomy,
    ConfidenceBand,
    DecisionPolicy,
    PolicyOutcome,
    RolloutStage,
    band_for,
)

__all__ = [
    "Answer",
    "Autonomy",
    "CONFIDENCE_BANDS",
    "ConfidenceBand",
    "DecisionError",
    "DecisionGateway",
    "DecisionPolicy",
    "DecisionProviderError",
    "DecisionProviderUnavailable",
    "DecisionQuestion",
    "DecisionRefused",
    "DecisionRequest",
    "DecisionResult",
    "InvalidDecisionResult",
    "NoDecisionProvider",
    "PolicyBlocked",
    "PolicyOutcome",
    "ProviderChain",
    "QuestionType",
    "RolloutStage",
    "UnknownQuestionType",
    "band_for",
    "build_gateway",
]
