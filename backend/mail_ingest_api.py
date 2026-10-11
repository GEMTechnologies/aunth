"""The doorway Cloudflare delivers to, and the only way mail enters Granada.

WHY A CLOUDFLARE WORKER AND NOT IMAP. The domain's catch-all is routed by Cloudflare Email Routing,
so every message for every NGO arrives at one Worker. Thousands of NGOs therefore need no mailbox
provisioning at all - the address is a row in `mail_identities`, and this endpoint resolves it. That
is the whole reason the per-NGO address is cheap.

THE POLICY THE CALLER IS TOLD TO APPLY. The Worker cannot decide anything; it reports, this endpoint
answers, and the Worker acts. Three outcomes, because collapsing them loses real mail:

  accept   -- stored, and offered to any verification request that matches.
  reject   -- a KNOWN address that is disabled, or an address that is not an NGO's at all. Telling
              Cloudflare to reject means the sender gets a bounce, which is honest: the mailbox does
              not exist, and silently dropping invites a retry loop from a legitimate sender.
  retry    -- a TEMPORARY backend failure. Deliberately NOT a rejection: a database blip must not
              bounce a funder's message. Cloudflare has its own retry, so the caller is told to let
              it and not to accept on our behalf.

AUTHENTICATION IS NOT OPTIONAL. The shared secret is empty by default, which disables the endpoint
rather than leaving an open door that writes into every organisation's mailbox.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from config import settings

logger = logging.getLogger("mail_ingest_api")

router = APIRouter(prefix="/internal/mail", tags=["internal-mail"])

MAX_BODY = 512_000

#: Overridable so the decision logic can be tested without a live database. Production leaves this
#: as None and the real session factory is used.
SESSION_FACTORY: Any = None


def _session() -> Any:
    from database import FleetSessionLocal

    return (SESSION_FACTORY or FleetSessionLocal)()
#: Addresses the system sends FROM and must never accept INTO. A reply to a no-reply address is not
#: a conversation, and accepting it would put it in an NGO's mailbox.
NEVER_A_MAILBOX = ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "abuse")


def require_ingest_key(x_ingest_key: str = Header(default="", alias="X-Ingest-Key")) -> str:
    configured = (getattr(settings, "mail_ingest_key", "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="mail ingestion is not configured: set MAIL_INGEST_KEY to enable it",
        )
    if not x_ingest_key or not secrets.compare_digest(x_ingest_key.strip(), configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid X-Ingest-Key header is required",
        )
    return "ingest"


class InboundIn(BaseModel):
    to: str
    sender: str = ""
    subject: str = ""
    text: str = ""
    html: str = ""
    message_id: str = ""
    received_at: Optional[datetime] = None


class InboundOut(BaseModel):
    action: str
    reason: str = ""
    org_id: Optional[str] = None
    address: Optional[str] = None
    stored_message_id: Optional[str] = None
    verification_ready: bool = False
    verification_kind: Optional[str] = None


def _local_part(address: str) -> str:
    return (address or "").split("@", 1)[0].strip().lower()


@router.post("/ingest", response_model=InboundOut, summary="Cloudflare Email Worker delivery")
def ingest(
    payload: InboundIn,
    _: str = Depends(require_ingest_key),
) -> InboundOut:
    import models
    from tenant_context import clear_tenant, set_tenant
    from agent.mail.addresses import DOMAIN
    from agent.mail.verification_service import VerificationService
    import uuid as _uuid

    recipient = (payload.to or "").strip().lower()
    if "@" not in recipient:
        return InboundOut(action="reject", reason="the message names no usable recipient address")

    local, _, host = recipient.rpartition("@")
    if host != DOMAIN:
        # Not our domain. Rejecting is right: we are not the mail system for it.
        return InboundOut(action="reject", reason=f"{host!r} is not a Granada mail domain")

    if any(local.startswith(bad) for bad in NEVER_A_MAILBOX):
        return InboundOut(action="reject", reason=f"{local!r} is a send-only address")

    if len(payload.text or "") + len(payload.html or "") > MAX_BODY:
        # Bounded, so a huge message cannot be used to exhaust the process.
        return InboundOut(action="reject", reason="the message exceeds the size this endpoint accepts")

    db = None
    try:
        # INSIDE the try, deliberately. Creating the session is the first thing that touches the
        # database, so an unreachable database would otherwise raise a 500 here instead of the
        # controlled `retry` - and a 500 with no body tells Cloudflare nothing about whether to
        # retry or bounce.
        db = _session()

        # The address is resolved WITHOUT a tenant binding, because that is the question being
        # asked: whose mailbox is this? It reads `mail_routing` rather than `mail_identities`,
        # because the latter is RLS-protected and with no binding `app.current_org()` is NULL, so a
        # direct query returns zero rows and EVERY delivery would be rejected as "no such mailbox".
        # Measured live before this was added.
        #
        # `mail_routing` holds address, org, identity and active - no tokens, no message content -
        # and is kept in sync by trigger, so routing never depends on application convention.
        resolved = db.execute(
            text("SELECT org_id, identity_id, is_active FROM mail_routing WHERE address = :addr"),
            {"addr": recipient},
        ).first()

        if resolved is None:
            # UNKNOWN RECIPIENT. Reported as a rejection so Cloudflare bounces it, rather than being
            # dropped silently: a legitimate sender who mistyped must find out.
            logger.info("mail_ingest_unknown_recipient", extra={"recipient": recipient[:120]})
            return InboundOut(action="reject", reason="no such mailbox on this domain")

        org_id, identity_id, active = resolved
        org_id = str(org_id)

        if not active:
            # A known but disabled mailbox. Same instruction to Cloudflare, different reason - the
            # operator can see which of the two happened.
            return InboundOut(action="reject", reason="this mailbox is not active")

        set_tenant(db, org_id, None)

        # NOW that the tenant is bound, the identity itself is readable - so the agent that owns this
        # mailbox comes from `mail_identities`, not from the routing table. Routing stays free of
        # anything but the route.
        identity_agent = db.execute(
            text("SELECT agent_id FROM mail_identities WHERE id = :id"),
            {"id": identity_id},
        ).scalar()

        received = payload.received_at or datetime.now(timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)

        # RETENTION FIRST. If verification handling fails, the message is still in the
        # organisation's records - losing the mail is worse than failing to act on it.
        message = models.MailMessage(
            id=str(_uuid.uuid4()),
            org_id=org_id,
            agent_id=str(identity_agent) if identity_agent else None,
            provider_message_id=(payload.message_id or f"cf:{received.timestamp()}")[:255],
            direction=models.MailMessage.DIRECTION_INBOUND,
            sender=payload.sender or None,
            recipients=[recipient],
            subject=payload.subject or None,
            body_preview=(payload.text or payload.html or "")[:500] or None,
            received_at=received,
            processing_status="RECEIVED",
            created_at=datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()

        # THEN offer it to whatever verification this organisation is waiting on.
        service = VerificationService(db, org_id=org_id)
        examination = service.examine(
            subject=payload.subject or "",
            body=payload.text or payload.html or "",
            sender=payload.sender or "",
            recipients=[recipient],
            received_at=received,
            message_id=message.id,
        )
        db.commit()

        return InboundOut(
            action="accept",
            reason=examination.outcome.reason,
            org_id=org_id,
            address=recipient,
            stored_message_id=message.id,
            verification_ready=bool(examination.satisfied or examination.already_satisfied),
            verification_kind=(
                examination.outcome.kind.value
                if (examination.satisfied or examination.already_satisfied)
                else None
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # TEMPORARY FAILURE, reported as such. Rejecting here would bounce a funder's message because
        # of a database blip, and accepting would claim we stored something we did not.
        if db is not None:
            db.rollback()
        logger.warning("mail_ingest_transient_failure", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return InboundOut(
            action="retry",
            reason="a temporary failure on Granada's side; the message was not stored and must be retried",
        )
    finally:
        if db is not None:
            clear_tenant(db)
            db.close()


@router.get("/ingest/health", summary="Whether ingestion is configured")
def ingest_health() -> dict[str, Any]:
    """Reports only whether the secret is set. Never the secret, and never the tenant list."""
    return {
        "configured": bool((getattr(settings, "mail_ingest_key", "") or "").strip()),
        "domain": "granadac.com",
        "actions": ["accept", "reject", "retry"],
    }
