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


class MailTenantMismatch(PermissionError):
    """A mailbox was offered to a gateway that does not own it.

    A `PermissionError` rather than a generic mail error, because the distinction matters to a caller:
    a provider failure means try again, while this means a caller reached for something it does not own
    and nothing about the request should be retried or reinterpreted.

    Named rather than inlined so an audit log, an alert and a test can all refer to the same condition.
    """

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

    IMAP IS BUILT ON DEMAND FROM SETTINGS, for the same reason SMTP is: nothing in production ever
    called `register_transport`, so every adapter was reachable from a test and from nowhere else.
    Setting `imap_host` is now genuinely enough.
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
            found = registry.get(provider) or registry.get(provider.upper())
            if found is not None:
                return found

        if provider.upper() == "IMAP":
            # Imported here: this module is imported by the scheduler, and a module-scope import would
            # put the IMAP stack on the fleet's start-up path whether or not mail is configured.
            from agent.mail.providers.imap import build_from_settings

            built = build_from_settings(settings)
            if built is not None:
                _TRANSPORTS["IMAP"] = built  # cached: one construction per process
            return built
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


def outbound_transport_for_account(
    db: Session, *, account: models.MailAccount, org_id: str
) -> Optional[Any]:
    """The OUTBOUND transport for ONE mailbox, built from that mailbox's OWN credential.

    The symmetric counterpart of `MailGateway.transport_for_account`, and it exists for the same reason:
    `get_outbound_transport(provider)` returns the DEPLOYMENT-WIDE transport registered under a provider
    name, so every organisation would send through whichever mailbox was registered. For inbound that
    means one tenant's mail delivered into another's pipeline; for outbound it means **one organisation's
    reply arriving from another organisation's address**, which is worse - it looks deliberate to the
    recipient.

    Order, matching the inbound method because the reasoning is the same:

      1. account belongs to another organisation -> REFUSE, before any lookup
      2. account HAS a credentials_ref          -> resolve it, and NEVER fall back
      3. account has no credentials_ref         -> the registered transport

    A module-level function rather than a method because the dispatcher resolves an outbound transport
    from a `MailSendIntent`, which carries `mail_account_id` and `provider` rather than a gateway.
    """
    if account.org_id != org_id:
        raise MailTenantMismatch(
            "refusing to resolve an outbound transport for a mailbox belonging to another organisation"
        )

    if account.credentials_ref:
        # A per-account credential ALWAYS wins, and there is NO fallback when it fails - see the inbound
        # method for why the order matters.
        payload = _stored_payload(db, account=account, org_id=org_id)
        if payload is None:
            return None
        provider = (account.provider or "").upper()
        if provider == "SMTP":
            from agent.mail.providers.smtp import SmtpConfig, SmtpOutboundMailProvider

            return SmtpOutboundMailProvider(
                SmtpConfig(
                    host=payload.get("host", ""),
                    port=int(payload.get("port") or 587),
                    username=payload.get("username", ""),
                    password=payload.get("password", ""),
                    use_starttls=bool(payload.get("use_tls", True)),
                    use_ssl=bool(payload.get("use_ssl", False)),
                )
            )
        logger.info(
            "mail.outbound_not_buildable",
            extra={"mail_account_id": account.id, "provider": provider},
        )
        return None

    return _OUTBOUND.get((account.provider or "").upper())


def _stored_payload(
    db: Session, *, account: models.MailAccount, org_id: str
) -> Optional[dict[str, Any]]:
    """Resolve one account's stored credential, or None on every failure.

    None rather than an exception for a missing, revoked or undecryptable credential, and for an
    unconfigured store: the caller's correct response is to park the work, and a failure that raised
    here would stop the fleet for one organisation's key rotation.
    """
    if not account.credentials_ref:
        return None
    try:
        from config import settings

        from agent.credential_store import store_from_settings

        store = store_from_settings(db, settings)
        return store.get(org_id=org_id, ref=account.credentials_ref)
    except Exception as exc:  # noqa: BLE001 - all four cases park the work
        logger.warning(
            "mail.outbound_credential_unavailable",
            extra={"mail_account_id": account.id, "error": type(exc).__name__},
        )
        return None


def get_outbound_transport(provider: str) -> Optional[Any]:
    """The OUTBOUND transport for a provider, or ``None``.

    ``None`` means "this mailbox cannot send", which the final authority check turns
    into a refusal with a name rather than an AttributeError. That distinction
    matters: one is a configuration state an operator fixes, the other looks like a
    bug and invites a workaround.

    SMTP IS BUILT ON DEMAND FROM SETTINGS. Nothing in production called
    `register_outbound_transport` - only tests did - so every adapter was reachable from a test and
    from nowhere else. A configuration-driven factory means setting `smtp_host` is genuinely enough,
    with no start-up hook to remember and no import-order dependency.
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
            found = registry.get(provider) or registry.get(provider.upper())
            if found is not None:
                return found

        if provider.upper() == "SMTP":
            # Imported here: `smtp.py` pulls in `outbound.py`, and this module is imported by the
            # scheduler. A module-scope import would put the whole mail stack on the fleet's start-up
            # path whether or not mail is configured.
            from agent.mail.providers.smtp import build_from_settings

            built = build_from_settings(settings)
            if built is not None:
                # Cached, so a configured SMTP transport is constructed once per process rather than
                # once per message.
                _OUTBOUND["SMTP"] = built
            return built
    except Exception:  # pragma: no cover - configuration is optional here
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

    def transport_for_account(self, account: models.MailAccount) -> Optional[Any]:
        """The inbound transport for ONE mailbox, built from that mailbox's OWN credential.

        WHY NOT `transport_for(account.provider)`

        That returns the deployment-wide transport registered under a provider name - one mailbox for
        the whole system. Every organisation would then read and send through the same account, which is
        the exact opposite of "a browser task for NGO A must never read NGO B's credentials".

        This resolves `account.credentials_ref` through the credential store, scoped to
        `self.org_id`. Three things follow:

          * an account belonging to ANOTHER organisation is refused before any lookup, so a forged
            `account` object cannot be used to reach across tenants
          * an account WITH a `credentials_ref` always uses it, and never falls back to a
            deployment-wide transport - the fallback would be a silent cross-tenant read
          * an account WITHOUT one gets the registered transport, which is a single-mailbox deployment
            or a test double rather than a shared multi-tenant one

        Returns None when the mailbox is configured but has no usable credential, matching
        `get_transport`'s contract: the caller parks the work rather than failing it.
        """
        if account.org_id != self.org_id:
            # Before the lookup, not after. A forged account object must not even reach the store.
            raise MailTenantMismatch(
                "refusing to resolve a transport for a mailbox belonging to another organisation"
            )

        if account.credentials_ref:
            # A PER-ACCOUNT CREDENTIAL WINS, and wins FIRST.
            #
            # The first version consulted the provider-wide registry before this, which meant a
            # deployment-wide transport silently bypassed per-account scoping - every organisation
            # sharing one mailbox again, through the front door this method exists to close. Found by a
            # test asserting that an account with no credential does not borrow one.
            #
            # There is NO fallback when the credential is missing, revoked or undecryptable. A fallback
            # here would be a silent cross-tenant read, which is the worst possible failure for this
            # method, so every failure returns None and the caller parks the work.
            return self._build_from_stored_credential(account)

        # No per-account credential. This is a single-mailbox deployment or a test double, and the
        # registered transport is the honest answer. It cannot be reached by an account that HAS a
        # credential, so it cannot displace one.
        return _TRANSPORTS.get((account.provider or "").upper())

    def _build_from_stored_credential(self, account: models.MailAccount) -> Optional[Any]:
        """Build the provider adapter named by the account, using its stored credential."""
        if not account.credentials_ref:
            logger.info(
                "mail.transport_no_credential_ref",
                extra={"mail_account_id": account.id, "provider": account.provider},
            )
            return None

        try:
            from config import settings

            from agent.credential_store import store_from_settings

            store = store_from_settings(self.db, settings)
        except Exception as exc:  # noqa: BLE001 - an unconfigured store is a deployment state
            logger.warning(
                "mail.credential_store_unavailable",
                extra={"mail_account_id": account.id, "error": type(exc).__name__},
            )
            return None

        try:
            payload = store.get(org_id=self.org_id, ref=account.credentials_ref)
        except Exception as exc:  # noqa: BLE001 - missing, revoked and undecryptable all land here
            # Logged WITHOUT the payload or the ref's content beyond the account id: a credential ref
            # reaching a log line is how a secret store becomes a list of targets.
            logger.warning(
                "mail.credential_unavailable",
                extra={"mail_account_id": account.id, "error": type(exc).__name__},
            )
            return None

        provider = (account.provider or "").upper()
        if provider == "IMAP":
            from agent.mail.providers.imap import ImapConfig, ImapInboundMailProvider

            return ImapInboundMailProvider(
                ImapConfig(
                    host=payload.get("host", ""),
                    port=int(payload.get("port") or 993),
                    username=payload.get("username", account.address),
                    password=payload.get("password", ""),
                    use_ssl=bool(payload.get("use_ssl", True)),
                    mailbox=payload.get("mailbox") or "INBOX",
                )
            )

        # GOOGLE and MICROSOFT adapters take a token rather than a password, and their construction is
        # a separate step with its own refresh path. Returning None is honest: this mailbox is
        # configured but its provider has no per-account builder yet, so the caller parks the work.
        logger.info(
            "mail.transport_not_buildable",
            extra={"mail_account_id": account.id, "provider": provider},
        )
        return None

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
        """Bounded reconciliation, because a webhook alone is not reliable.

        USES `transport_for_account`, NOT `transport_for(account.provider)`. The latter returned the
        deployment-wide transport registered under a provider name, so every organisation's
        reconciliation would have read whichever mailbox was registered - one tenant's mail delivered
        into another tenant's pipeline. The per-account method resolves that mailbox's OWN credential and
        refuses an account belonging to a different organisation.
        """
        from agent.mail.service import GranadaMail

        transport = self.transport_for_account(account)
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
    db: Session, *, stale_seconds: int = 900, batch_size: int = 200, org_id: Optional[str] = None
) -> int:
    """Find mailboxes due for reconciliation and create durable work for each.

    **One shared scheduler, and it only discovers.** There is deliberately no timer
    per mailbox, no process per mailbox and no scheduler per GranadaAgent: a timer
    per mailbox is ten thousand timers, and a mail account is a row rather than a
    service. This queries for stale accounts in one statement and enqueues a
    ``mail_sync`` workflow for each; the shared fleet executes them.

    The work is bounded by ``batch_size`` so a fleet with a hundred thousand
    mailboxes cannot load them all into one transaction.

    ``org_id`` SCOPES THE SWEEP TO ONE ORGANISATION, and it exists for ADR-0011.
    The query below crosses tenants by default, which only works while the fleet
    connection carries BYPASSRLS. With binding on, `FleetRunner` calls this once per
    organisation with `app.current_org_id` set, and the scope argument makes the
    statement match the binding rather than fight it. Without it the query returns
    zero rows under RLS and **mail reconciliation stops silently** - a mailbox that
    never syncs again produces no error, only an absence.

    Returned count is how many workflows were newly created. A mailbox already
    queued is not queued twice - the workflow uniqueness constraint on
    ``(agent_id, workflow_type, subject_type, subject_id)`` decides that, not this
    function, so two schedulers racing still produce one workflow.
    """
    from datetime import timedelta

    from agent.workflow_engine import WORKFLOW_MAIL_SYNC

    cutoff = _now() - timedelta(seconds=max(60, int(stale_seconds)))
    stale = select(models.MailAccount).where(
        models.MailAccount.status == models.MailAccount.ACTIVE,
        # A mailbox that has never synced is due immediately; one that synced
        # recently is not. NULL is not "unknown", it is "never".
        (models.MailAccount.last_sync_at.is_(None))
        | (models.MailAccount.last_sync_at < cutoff),
    )
    if org_id is not None:
        stale = stale.where(models.MailAccount.org_id == org_id)
    candidates = db.execute(
        stale.order_by(models.MailAccount.last_sync_at.asc().nullsfirst()).limit(batch_size)
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
