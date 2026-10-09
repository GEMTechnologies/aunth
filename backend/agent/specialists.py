"""The specialist registry: a closed set of executable handlers.

**Nothing here is imported from a job or a message.** The brief is explicit, and
the reason is the same one that makes the agent-routing allowlist closed: a job
payload is transport, and if a payload could name a handler then a forged message
would be arbitrary code execution against a customer's data. A specialist is
resolved by looking its key up in :data:`REGISTRY`, and an unknown key is an
error rather than a lookup.

What a specialist declares
--------------------------
``allowed_work_types``
    Which workflow types it may run. A mismatch is refused, so a
    ``donor_research`` job cannot be executed by the proposal writer because
    somebody typed the wrong key.
``required_authority``
    The minimum the **agent** must hold. Checked against the agent's level by
    ``GranadaAgentService.require_authority`` - the parent is the ceiling.
``enabled``
    Phase 6c implements three. The other seven are **registered and disabled**,
    which is honest: the roster exists so the customer can see it, and a
    specialist with no handler must refuse rather than silently succeed at
    nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from agent.decision.policy import Autonomy

logger = logging.getLogger(__name__)


class SpecialistError(RuntimeError):
    """Base class for specialist failures."""


class UnknownSpecialist(SpecialistError):
    """A key that is not in the registry.

    Raised rather than defaulted. A job naming a specialist nobody implemented
    must fail loudly, not quietly do nothing while the workflow moves on.
    """


class SpecialistDisabled(SpecialistError):
    """A registered specialist with no handler in this phase."""


class WorkTypeNotAllowed(SpecialistError):
    """The specialist may not run that workflow type."""


@dataclass(frozen=True)
class SpecialistSpec:
    """One specialist's declaration.

    Handlers are stored as **names** and resolved lazily. Resolving at import time
    created a circular import (the registry needs the engine, the engine needs the
    registry) and lazy resolution removes it without weakening the rule that the
    name comes from *this file* rather than from data.

    ``handlers`` maps work type to handler name, so one specialist can own more
    than one kind of step. The Matching Agent owns both ``opportunity_match`` (the
    deterministic gates) and ``opportunity_qualify`` (the bounded decision),
    because the brief's roster has ten specialists and the customer should not see
    an eleventh that exists only to hold one function.
    """

    key: str
    display_name: str
    allowed_work_types: frozenset[str]
    required_authority: str
    #: work type -> handler function name. Empty means registered but not
    #: implemented, which is a different thing from missing and is reported so.
    handlers: Mapping[str, str] = field(default_factory=dict)
    version: str = "v1"

    @property
    def enabled(self) -> bool:
        return bool(self.handlers)

    def accepts(self, workflow_type: str) -> bool:
        return workflow_type in self.allowed_work_types

    def load(self, workflow_type: str) -> Callable[..., Any]:
        """Resolve the handler for a work type, by explicit name, from one module.

        Never by importing a string that came from a job payload: a payload is
        transport, and if it could name a handler then a forged message would be
        arbitrary code execution against a customer's data.
        """
        handler_name = self.handlers.get(workflow_type)
        if handler_name is None:
            raise SpecialistDisabled(
                f"{self.display_name} ({self.key}) has no handler for "
                f"{workflow_type!r} in this phase"
            )
        from agent import workflow_engine

        resolved = getattr(workflow_engine, handler_name, None)
        if resolved is None:  # pragma: no cover - a typo in this file
            raise SpecialistError(
                f"handler {handler_name!r} is declared for {self.key} but not defined"
            )
        return resolved


def _spec(
    key: str,
    display_name: str,
    handlers: Mapping[str, str],
    authority: str,
    registered_work_types: tuple[str, ...] = (),
) -> SpecialistSpec:
    """Build a spec. ``registered_work_types`` covers a specialist whose work
    types are known but whose handler is not written yet."""
    work_types = frozenset(handlers) | frozenset(registered_work_types)
    return SpecialistSpec(
        key=key,
        display_name=display_name,
        allowed_work_types=work_types,
        required_authority=authority,
        handlers=dict(handlers),
    )


#: The closed registry: exactly the ten specialists the roster names, so the
#: customer's view and the executable set are the same list.
REGISTRY: dict[str, SpecialistSpec] = {
    spec.key: spec
    for spec in (
        _spec(
            "OPPORTUNITY_HUNTER", "Funding Hunter", {},
            Autonomy.MONITOR_ONLY, registered_work_types=("opportunity_scan",),
        ),
        # Implemented in Phase 6c. Owns both the deterministic gates and the
        # bounded decision, because the roster has ten specialists and an eleventh
        # existing only to hold one function would be roster noise.
        _spec(
            "MATCHER", "Matching Agent",
            {
                "opportunity_match": "_handle_match",
                "opportunity_qualify": "_handle_qualify",
            },
            Autonomy.MONITOR_ONLY,
        ),
        # Implemented in Phase 6c.
        _spec(
            "DONOR_RESEARCHER", "Donor Research Agent",
            {"donor_research": "_handle_donor_research"},
            Autonomy.MONITOR_ONLY,
        ),
        _spec("PROPOSAL_WRITER", "Proposal Agent", {}, Autonomy.DRAFT_ONLY,
              registered_work_types=("proposal_draft",)),
        _spec("BUDGET", "Budget Agent", {}, Autonomy.DRAFT_ONLY,
              registered_work_types=("budget_prepare",)),
        # Assembly is a compliance act: it checks that what the funder requires is present and
        # frozen before anything reaches them. MONITOR_ONLY suffices because this handler produces a
        # package for a human to authorise and performs no external action.
        _spec(
            "COMPLIANCE", "Compliance Agent",
            {
            "application_assemble": "_handle_package_assemble",
            # Section 6: the browser runs as a leased capability through the same job system. The
            # handler PARKS when the flag is off, so registering it does not enable it - and an NGO
            # whose package is merely unfinished is not an infrastructure failure.
            "browser_task": "_handle_browser_task",
        },
            Autonomy.MONITOR_ONLY,
            registered_work_types=("compliance_check",),
        ),
        # Implemented now: the handler existed nowhere before, so the roster showed a Document
        # Agent that could not produce a document.
        #
        # DRAFT_ONLY, not MONITOR_ONLY. The output of this specialist is a draft a human must
        # approve - it writes documents to the vault in a PENDING state and cannot advance them.
        # Granting it more authority than the artefact deserves is how an agent ends up submitting
        # its own unreviewed work under an organisation's name.
        _spec(
            "DOCUMENT", "Document Agent",
            {"document_generate": "_handle_document_generate"},
            Autonomy.DRAFT_ONLY,
            registered_work_types=("document_request",),
        ),
        # Implemented in Phase 7a — EARS ONLY.
        #
        # `mail_process` has a handler. `email_send` is listed as a REGISTERED work
        # type and deliberately has none, so the roster shows the customer that the
        # capability exists while the code makes it impossible to dispatch. That is
        # the ceiling expressed in the registry rather than in a comment: a
        # dispatcher cannot enqueue what no handler owns, and the capability assert
        # would refuse it even if it could.
        _spec(
            "EMAIL", "Email Agent",
            {
                "mail_process": "_handle_mail_process",
                "mail_reconcile": "_handle_mail_reconcile",
                "mail_sync": "_handle_mail_sync",
                # `mail_send` HAS a handler, and that is not a contradiction of the
                # Phase 7b rule. The handler does not decide to send: it loads a
                # send intent that a human already approved and revalidates it. An
                # intent with no live matching approval is refused, so the work type
                # being executable does not make autonomous sending possible.
                "mail_send": "_handle_mail_send",
            },
            Autonomy.MONITOR_ONLY,
            registered_work_types=(
                "mail_process", "mail_sync", "mail_reconcile", "mail_send",
                # Still listed, still WITHOUT a handler, still undispatchable.
                "email_send",
            ),
        ),
        _spec("SUBMISSION", "Submission Agent", {}, Autonomy.AUTOPILOT_WITH_GATES,
              registered_work_types=("submission",)),
        _spec("FOLLOW_UP", "Follow-up Agent", {}, Autonomy.AUTO_ROUTINE,
              registered_work_types=("follow_up",)),
    )
}

#: The specialists that can actually execute in this phase.
EXECUTABLE = frozenset(key for key, spec in REGISTRY.items() if spec.enabled)

#: Registered because the customer should see them; disabled because Phase 6c
#: deliberately implements three. Named so the honesty is in the code.
NOT_YET_IMPLEMENTED = frozenset(key for key, spec in REGISTRY.items() if not spec.enabled)


def resolve(key: str) -> SpecialistSpec:
    """Look a specialist up. Unknown keys are refused, never defaulted."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise UnknownSpecialist(
            f"{key!r} is not a registered specialist; the registry is closed and "
            f"its keys are {sorted(REGISTRY)}"
        )
    return spec


def require_executable(key: str) -> SpecialistSpec:
    """Resolve a specialist that must be able to run right now."""
    spec = resolve(key)
    if not spec.enabled:
        raise SpecialistDisabled(
            f"{spec.display_name} ({key}) is registered but not implemented in this "
            "phase; it must refuse rather than appear to succeed at nothing"
        )
    return spec


def for_work_type(workflow_type: str) -> list[SpecialistSpec]:
    """Every specialist that may run a workflow type. Used to build workflows."""
    return [spec for spec in REGISTRY.values() if spec.accepts(workflow_type)]


def check_work_type(key: str, workflow_type: str) -> SpecialistSpec:
    """Resolve and verify the pairing in one step.

    Both halves matter: an unknown specialist and a specialist asked to do work it
    does not do are different failures, and the message says which.
    """
    spec = resolve(key)
    if not spec.accepts(workflow_type):
        raise WorkTypeNotAllowed(
            f"{key} may not run {workflow_type!r}; it accepts "
            f"{sorted(spec.allowed_work_types)}"
        )
    return spec


def inventory() -> dict[str, Any]:
    """The registry as data, for the runbook and the dashboard."""
    return {
        "total": len(REGISTRY),
        "executable": sorted(EXECUTABLE),
        "registered_not_implemented": sorted(NOT_YET_IMPLEMENTED),
        "specialists": [
            {
                "key": spec.key,
                "display_name": spec.display_name,
                "enabled": spec.enabled,
                "required_authority": spec.required_authority,
                "allowed_work_types": sorted(spec.allowed_work_types),
                "version": spec.version,
            }
            for spec in REGISTRY.values()
        ],
    }
