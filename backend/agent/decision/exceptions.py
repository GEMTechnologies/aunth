"""Decision Gateway exceptions.

Separate from the other agent modules because the failure taxonomy here is what
the fallback and circuit-breaker logic keys off. "Unavailable" and "refused" and
"returned nonsense" have to be distinguishable, or the fallback cannot pick the
right response: an outage should fall back, a refusal should escalate to a human,
and an invalid result should never be silently coerced into an answer.
"""

from __future__ import annotations


class DecisionError(RuntimeError):
    """Base class for decision failures."""


class NoDecisionProvider(DecisionError):
    """No provider is configured or available for this decision.

    Raised rather than returning a default. A gateway that quietly answers
    "UNKNOWN" when nothing is wired up makes an unwired system look like a
    cautious one.
    """


class DecisionProviderUnavailable(DecisionError):
    """The provider could not be reached, timed out, or is circuit-broken.

    Retryable and fallback-eligible. Distinct from a refusal: this says nothing
    about the decision itself.
    """


class DecisionProviderError(DecisionError):
    """The provider was reached and returned an error."""


class InvalidDecisionResult(DecisionError):
    """The provider returned something that does not satisfy the question schema.

    Never coerced. A decision model that answers "PROBABLY_YES" to a question
    whose options are YES/NO has not answered the question, and treating its
    nearest-looking option as the answer is how a wrong classification becomes a
    real action. The brief's rule - model output is untrusted until validated -
    applies here exactly as it does to generated prose.
    """


class UnknownQuestionType(DecisionError):
    """A question type Granada does not model was requested."""


class DecisionRefused(DecisionError):
    """The provider declined to answer.

    A refusal is information. It routes to a human, not to a fallback provider,
    because asking a second model the same question until one of them is willing
    to answer is not a decision procedure.
    """


class PolicyBlocked(DecisionError):
    """Granada's own policy refused the action, whatever the provider answered.

    This is the error that proves the code owns permission: it can be raised for
    a decision the provider answered confidently and correctly.
    """
