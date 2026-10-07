"""The provider contract.

One method. Everything a decision provider must do is answer typed questions
about a supplied state, and every additional method here would be a place where
one vendor's capabilities leak into the business layer - which is precisely what
the brief forbids.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.decision.models import DecisionRequest, DecisionResult


@runtime_checkable
class DecisionProvider(Protocol):
    """What Granada needs from any decision source."""

    #: Stable identifier recorded on every result and audit entry.
    name: str

    #: True when this provider is configured well enough to attempt a call.
    #: Checked before selection so an unconfigured provider is skipped rather
    #: than attempted and failed - the two look identical in a metric otherwise.
    available: bool

    def decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        """Answer the request's questions, or raise.

        Implementations must raise rather than return a guessed answer. The
        gateway's fallback logic keys on the exception type, so a provider that
        returns its best effort silently removes Granada's ability to tell
        "answered" from "failed".
        """
        ...


class BaseDecisionProvider:
    """Convenience base: a name, an availability flag, and the answer builder.

    Subclasses implement ``_decide``. Providers that are not configured set
    ``available = False`` and are skipped by the chain.
    """

    name = "base"
    available = False

    def decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        if not self.available:
            from agent.decision.exceptions import DecisionProviderUnavailable

            raise DecisionProviderUnavailable(f"provider {self.name!r} is not available")
        return self._decide(request, timeout_seconds=timeout_seconds)

    def _decide(self, request: DecisionRequest, *, timeout_seconds: int) -> DecisionResult:
        raise NotImplementedError
