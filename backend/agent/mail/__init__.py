"""Granada Mail: an organisation agent's ears. Phase 7a has no voice.

Receive, understand, link, draft. **No send path exists.**

The mail subsystem belongs to the `GranadaAgent`, not to a separate product: every
mail record carries the same ``(agent_id, org_id)`` pair as the rest of the fleet
and is protected by the same composite foreign key, so a mail row cannot say
"org A, agent B" any more than a job can. Mail arrives as one more wake condition
for the shared fleet - there is no per-mailbox worker, no per-NGO mail process and
no mail daemon.

Module map, and why each boundary sits where it does:

``vocabulary``
    Capability ceiling, classifications, correlation states. The ceiling is one
    list here rather than a rule repeated at every call site.
``security``
    Email is hostile input. Deterministic screening, provenance checks, and the
    single place model input is built - so the untrusted-data boundary is explicit
    rather than a convention each caller remembers.
``providers.base``
    The transport protocol. Note what is absent: no ``send``, no ``reply``, no
    ``forward``. The capability ceiling expressed in types.
``providers.fake``
    Deterministic provider for tests.
``correlation``
    Which application a message belongs to. Wrong linkage is worse than no linkage,
    so the outcome is a state with a reason rather than a best guess.
``classification``
    Rules first, always; deadlines extracted as durable work; approved-document
    lookup.
``service``
    The pipeline: event, fetch, dedupe, store, thread, screen, classify, deadline,
    correlate, document, draft, in one transaction with its events.
"""

from agent.mail.vocabulary import (
    Capability,
    CapabilityRefused,
    CorrelationState,
    DocumentRequestType,
    DraftStatus,
    ExternalActionDisabled,
    LinkMethod,
    MailClassification,
    SecurityFlag,
    assert_capability,
)

__all__ = [
    "Capability",
    "CapabilityRefused",
    "CorrelationState",
    "DocumentRequestType",
    "DraftStatus",
    "ExternalActionDisabled",
    "LinkMethod",
    "MailClassification",
    "SecurityFlag",
    "assert_capability",
]