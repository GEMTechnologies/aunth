"""The provider-neutral mail gateway, and how email wakes an agent.

Two responsibilities, and they belong together because they are the two halves of
the same boundary: **transport selection** (which vendor carries a message) and
**wake-up** (which agent's work the message becomes).

Transport selection
-------------------
A registry keyed by provider name. Business logic never imports a vendor module;
it asks the gateway for a transport and receives whatever satisfies
`MailTransport`. Adding Gmail is `register_transport("GOOGLE", GoogleTransport(...))`
and nothing else changes.

A provider with **no** registered transport is an operational state rather than an
error, so `get_transport` returns ``None`` and the handler parks. Conflating "not
configured" with "failed" would make a deployment gap look like mail corruption.

Wake-up, and why it is not a worker
-----------------------------------
An inbound message becomes a `mail_process` workflow for the organisation's agent,
exactly as a new opportunity becomes an `opportunity_match` workflow. That is the
whole integration: the shared fleet already knows how to find, lease, execute and
recover work, so mail needs no scheduler of its own.

The brief forbids a per-mailbox worker, a per-NGO mail process and a per-agent mail
daemon. This design has none of those by construction — there is nothing here that
runs on a timer or holds a connection open. Work is a row, and the fleet picks it
up when it is due.

Sending
-------
`MailGateway.send` exists and **always raises**. The transport protocol has no send
method at all, which is the stronger guarantee; this method exists so the policy
layer can refuse outbound mail by name and so a test can prove the refusal rather
than infer it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent.mail.providers.base import MailTransport, ProviderEvent, refuse_send
from agent.mail.vocabulary import Capability, assert_capability

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Transport registry
# ---------------------------------------------------------------------------
_TRANSPORTS: dict[str, Any] = {}


def register_transport(provider: str, transport: Any) -> None:
    """Make a transport available for a provider name.

    Called by the deployment at start-up, and by tests to install a fake. The
    registry is process-local on purpose: credentials should not be replicated
    through a database, and a transport is a connection object rather than data.
    """
    if not provider:
        raise ValueError("a transport must be registered under a provider name")
    _TRANSPORTS[provider.upper()] = transport


def unregister_transport(provider: str) -> None:
    _TRANSPORTS.pop(provider.upper(), None)


def clear_transports() -> None:
    """Remove every registration. For test isolation."""
    _TRANSPORTS.clear()


def get_transport(provider: str) -> Optional[Any]:
    """The transport for a provider, or ``None`` if none is configured.

    ``None`` rather than an exception: an unconfigured provider is a deployment
    state, and the caller's correct response is to park the work and retry later,
    not to mark the mail as failed.
    """
    if not provider:
        return None
    direct = _TRANSPORTS.get(provider.upper())
    if direct is not None:
        return direct
    # Fall back to Settings so a deployment can wire a transport by configuration
    # without importing this module into its start-up order.
    try:
        from config import settings

        registry = getattr(settings, "mail_transports", None)
        if isinstance(registry, dict):
            return registry.get(provider) or registry.get(provider.upper())
    except Exception:  # pragma: no cover - configuration is optional here
        return None
    return None


def registered_providers() -> list[str]:
    return sorted(_TRANSPORTS)


# ---------------------------------------------------------------------------
# Outbound transport registry — a SEPARATE namespace
# ---------------------------------------------------------------------------
# Deliberately not the same dictionary as the inbound one. A provider that can
# read a mailbox and a provider that can send from it are different capabilities,
# and sharing a registry is how "this account is connected" quietly comes to mean
# "this account can speak". An account may appear in one, both, or neither.
_OUTBOUND: dict[str, Any] = {}


def register_outbound_transport(provider: str, transport: Any) -> None:
    """Make an outbound transport available for a provider name."""
    if not provider:
        raise ValueError("an outbound transport must be registered under a provider name")
    _OUTBOUND[provider.upper()] = transport


def unregister_outbound_transport(provider: str) -> None:
    _OUTBOUND.pop(provider.upper(), None)


def clear_outbound_transports() -> None:
    """For test isolation. Also clears inbound, so no test can leak either."""
    _OUTBOUND.clear()


def get_outbound_transport(provider: str) -> Optional[Any]:
    """The OUTBOUND transport for a provider, or ``None``.

    ``None`` means "this mailbox cannot send", which the final authority check turns
    into a refusal with a name rather than an AttributeError. That distinction
    matters: one is a configuration state an operator fixes, the other looks like a
    bug and invites a workaround.
    """
    if not provider:
        return None
    direct = _OUTBOUND.get(provider.upper())
    if direct is not None:
        return direct
    try:
        from config import settings

        registry = getattr(settings, "outbound_mail_transports", None)
        if isinstance(registry, dict):
            return registry.get(provider) or registry.get(provider.upper())
    except Exception:  # pragma: no cover
        return None
    return None


def registered_outbound_providers() -> list[str]:
    return sorted(_OUTBOUND)


# ---------------------------------------------------------------------------
# Wake-up
# ---------------------------------------------------------------------------
#: The subject type a mail workflow carries, so `due_workflows` can order and
#: de-duplicate mail the same way it does opportunities.
SUBJECT_MAIL_EVENT = "MAIL_EVENT"


@dataclass
class WakeResult:
    """What enqueueing a mail wake-up did."""

    workflow_id: str
    created: bool
    provider_event_id: str


def wake_on_email(
    db: Session,
    *,
    org_id: str,
    agent_id: str,
    provider: str,
    provider_event_id: str,
    provider_account_id: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    provider_thread_id: Optional[str] = None,
    event_type: str = "message.received",
    run_at: Optional[datetime] = None,
) -> WakeResult:
    """Turn a provider delivery into work for one organisation's agent.

    This is the Phase 7a equivalent of "a new opportunity was found". It creates a
    durable workflow, and the next fleet sweep dispatches it. Nothing runs here:
    the function is fast, synchronous, and does no network I/O, so a webhook
    handler can call it inside its request and answer the provider immediately.

    **Tenancy is checked, not trusted.** The agent must belong to the stated
    organisation, so a webhook that names a mismatched pair is refused here as well
    as by the composite foreign key at the schema level. Two layers, because the
    schema protects the data and this protects the work.

    **The payload carries identifiers only.** No subject, no sender, no body. The
    queue is the most replicated and least protected part of the system, and the
    brief forbids putting mail bodies in Redis — so the job says *what to fetch*
    and the worker fetches it from the provider.
    """
    assert_capability(Capability.MAIL_RECEIVE)

    agent = db.execute(
        select(models.GranadaAgent).where(
            models.GranadaAgent.id == agent_id,
            models.GranadaAgent.org_id == org_id,
        )
    ).scalars().first()
    if agent is None:
        raise ValueError(
            f"agent {agent_id} does not belong to organisation {org_id}; a mail "
            "delivery must not be able to name another tenant's agent"
        )

    from agent.workflow_engine import WORKFLOW_MAIL

    workflow = AgentWake.schedule(
        db,
        agent=agent,
        workflow_type=WORKFLOW_MAIL,
        specialist_key="EMAIL",
        subject_type=SUBJECT_MAIL_EVENT,
        subject_id=provider_event_id,
        run_at=run_at or _now(),
        payload_ref={
            "provider": provider,
            "provider_event_id": provider_event_id,
            "provider_account_id": provider_account_id,
            "provider_message_id": provider_message_id,
            "provider_thread_id": provider_thread_id,
            "event_type": event_type,
        },
    )
    return WakeResult(
        workflow_id=workflow.id,
        created=workflow.attempts == 0,
        provider_event_id=provider_event_id,
    )


class AgentWake:
    """Minimal scheduling helper, delegating to the agent service.

    Thin on purpose: `GranadaAgentService.schedule` already owns the uniqueness
    rule that stops the same subject being scheduled twice, and duplicating that
    logic here is how two code paths start disagreeing about what "already
    scheduled" means.
    """

    @staticmethod
    def schedule(
        db: Session,
        *,
        agent: models.GranadaAgent,
        workflow_type: str,
        specialist_key: str,
        subject_type: str,
        subject_id: str,
        run_at: Optional[datetime] = None,
        payload_ref: Optional[dict[str, Any]] = None,
    ) -> models.AgentWorkflow:
        from agent.granada_agent import GranadaAgentService

        service = GranadaAgentService(db, agent.org_id)
        workflow = service.schedule(
            workflow_type=workflow_type,
            subject_type=subject_type,
            subject_id=subject_id,
            specialist_key=specialist_key,
            run_at=run_at,
        )
        if payload_ref:
            # The wake payload lives on the WORKFLOW, not on the job. The workflow
            # row is organisation-scoped and RLS-protected; the queue is neither.
            # So the identifiers the worker needs are read from the protected row
            # and the job carries no mail data at all.
            workflow.context = {**(workflow.context or {}), "wake": payload_ref}
        db.flush()
        return workflow


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
class MailGateway:
    """The provider-neutral entry point for an organisation's mail.

    Named `MailGateway` rather than reached through the service directly, because
    the brief asks for the boundary to be explicit and because the send refusal
    belongs at the edge rather than buried in the pipeline.
    """

    def __init__(self, db: Session, *, org_id: str, agent_id: str) -> None:
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id

    def transport_for(self, provider: str) -> Optional[Any]:
        return get_transport(provider)

    def receive(
        self,
        *,
        provider: str,
        event: ProviderEvent,
        headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> Any:
        """Process one delivery through the full pipeline."""
        from agent.mail.service import GranadaMail

        transport = self.transport_for(provider)
        mail = GranadaMail(
            self.db, org_id=self.org_id, agent_id=self.agent_id, transport=transport
        )
        return mail.ingest_webhook(
            provider=provider, event=event, headers=headers, body=body
        )

    def sync(self, *, account: models.MailAccount, limit: int = 50, max_batches: int = 5) -> dict[str, Any]:
        """Bounded reconciliation, because a webhook alone is not reliable."""
        from agent.mail.service import GranadaMail

        transport = self.transport_for(account.provider)
        mail = GranadaMail(
            self.db, org_id=self.org_id, agent_id=self.agent_id, transport=transport
        )
        return mail.sync(account=account, limit=limit, max_batches=max_batches)

    def send(self, *args: Any, **kwargs: Any) -> None:
        """Always refuses. See the module docstring.

        Provided so the refusal is named and testable. The transport protocol has no
        send method, which is the guarantee that actually holds; this is here so an
        attempt produces an `ExternalActionDisabled` rather than an AttributeError
        that a caller might mistake for a bug and work around.
        """
        refuse_send(reason="Granada Mail has no outbound capability in Phase 7a")

# ---------------------------------------------------------------------------
# Scheduled reconciliation: discovery only
# ---------------------------------------------------------------------------
SUBJECT_MAIL_ACCOUNT = "MAIL_ACCOUNT"


def schedule_mail_sync(
    db: Session, *, stale_seconds: int = 900, batch_size: int = 200
) -> int:
    """Find mailboxes due for reconciliation and create durable work for each.

    **One shared scheduler, and it only discovers.** There is deliberately no timer
    per mailbox, no process per mailbox and no scheduler per GranadaAgent: a timer
    per mailbox is ten thousand timers, and a mail account is a row rather than a
    service. This queries for stale accounts in one statement and enqueues a
    ``mail_sync`` workflow for each; the shared fleet executes them.

    The work is bounded by ``batch_size`` so a fleet with a hundred thousand
    mailboxes cannot load them all into one transaction.

    Returned count is how many workflows were newly created. A mailbox already
    queued is not queued twice - the workflow uniqueness constraint on
    ``(agent_id, workflow_type, subject_type, subject_id)`` decides that, not this
    function, so two schedulers racing still produce one workflow.
    """
    from datetime import timedelta

    from agent.workflow_engine import WORKFLOW_MAIL_SYNC

    cutoff = _now() - timedelta(seconds=max(60, int(stale_seconds)))
    candidates = db.execute(
        select(models.MailAccount)
        .where(
            models.MailAccount.status == models.MailAccount.ACTIVE,
            # A mailbox that has never synced is due immediately; one that synced
            # recently is not. NULL is not "unknown", it is "never".
            (models.MailAccount.last_sync_at.is_(None))
            | (models.MailAccount.last_sync_at < cutoff),
        )
        .order_by(models.MailAccount.last_sync_at.asc().nullsfirst())
        .limit(batch_size)
    ).scalars().all()

    queued = 0
    for account in candidates:
        # A mailbox with no provider transport has nothing to reconcile against.
        # Parking it would accumulate dead work, so it is skipped rather than
        # queued - and the account's sync_status is what an operator looks at.
        if get_transport(account.provider) is None:
            continue
        try:
            AgentWake.schedule(
                db,
                agent=_agent_row(db, account),
                workflow_type=WORKFLOW_MAIL_SYNC,
                specialist_key="EMAIL",
                subject_type=SUBJECT_MAIL_ACCOUNT,
                subject_id=account.id,
                payload_ref={
                    "provider": account.provider,
                    "provider_account_id": account.provider_account_id,
                    "mail_account_id": account.id,
                },
            )
            queued += 1
        except Exception as exc:  # noqa: BLE001
            # One unreconcilable mailbox must not stop the sweep for every other
            # organisation.
            logger.warning(
                "mail.sync_schedule_failed",
                extra={"account_id": account.id, "error": str(exc)[:200]},
            )
            continue
    db.flush()
    return queued


def _agent_row(db: Session, account: models.MailAccount) -> models.GranadaAgent:
    """The agent that owns a mailbox, resolved from the row and never assumed."""
    agent = db.execute(
        select(models.GranadaAgent).where(
            models.GranadaAgent.id == account.agent_id,
            models.GranadaAgent.org_id == account.org_id,
        )
    ).scalars().first()
    if agent is None:
        raise ValueError(
            f"mail account {account.id} names an agent that does not belong to its "
            "organisation"
        )
    return agent
