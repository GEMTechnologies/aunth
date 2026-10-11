"""Matching incoming mail to a verification Granada is waiting for, and satisfying it once.

THE THREE THINGS THIS MUST GET RIGHT, in order of how badly they fail:

1. NEVER SATISFY THE WRONG REQUEST. A code is applied to a request only if the message arrived at
   that request's mailbox, came from that request's portal, and landed inside its window. Two
   organisations registering on the same portal at the same time is the normal case, not an exotic
   one, and the requests must not cross.
2. SATISFY EXACTLY ONCE. The fingerprint is UNIQUE(org_id, fingerprint) at the database, so a
   duplicate delivery, a retried job or two workers racing all resolve to one satisfaction. The
   IntegrityError IS the mechanism - not a pre-check, which would race.
3. RECORD WHY NOTHING HAPPENED. A request that silently stays PENDING is indistinguishable from a
   broken pipeline. `examined` and `last_reason` are what let an operator tell "the mail has not
   arrived" from "the mail arrived and was not usable".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy.exc import IntegrityError

import models
from agent.mail.verification import (
    VerificationKind,
    VerificationOutcome,
    VerificationRequest,
    extract,
)

logger = logging.getLogger("mail.verification")

DEFAULT_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class Examination:
    """What examining one message did, so the caller can report rather than guess."""

    outcome: VerificationOutcome
    request_id: Optional[str] = None
    satisfied: bool = False
    already_satisfied: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Attach UTC when a value read back from the database carries no zone.

    SQLite returns naive datetimes even for `DateTime(timezone=True)`, while every comparison here is
    against an aware `now`. Without this, the window checks raise TypeError on SQLite and pass on
    Postgres - a defect that would only appear in one environment.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class VerificationService:
    """Creates verification requests and satisfies them from arriving mail."""

    def __init__(self, db: Any, *, org_id: str) -> None:
        if not org_id:
            # Tenant unknown is a deny. Without it a request could be satisfied across organisations.
            raise ValueError("org_id is required to handle verification; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id

    # ------------------------------------------------------------------
    def request(
        self,
        *,
        address: str,
        portal_host: str,
        purpose: str,
        browser_task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> models.MailVerificationRequest:
        """Record that Granada has triggered a verification and is now waiting for it."""
        moment = _now()
        row = models.MailVerificationRequest(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=agent_id,
            address=(address or "").strip(),
            portal_host=(portal_host or "").strip().lower(),
            purpose=purpose[:120],
            browser_task_id=browser_task_id,
            status=models.MailVerificationRequest.PENDING,
            requested_at=moment,
            expires_at=moment + ttl,
            examined=0,
            created_at=moment,
        )
        self.db.add(row)
        self.db.flush()
        logger.info(
            "verification_requested",
            extra={"org_id": self.org_id, "request_id": row.id,
                   "portal_host": row.portal_host, "purpose": row.purpose},
        )
        return row

    # ------------------------------------------------------------------
    def pending(self, *, limit: int = 20) -> list[models.MailVerificationRequest]:
        """Requests still waiting, oldest first. Expiry is applied here rather than by a sweeper, so
        a request cannot be satisfied by a late code simply because nothing had cleaned it up."""
        moment = _now()
        rows = (
            self.db.query(models.MailVerificationRequest)
            .filter(
                models.MailVerificationRequest.org_id == self.org_id,
                models.MailVerificationRequest.status == models.MailVerificationRequest.PENDING,
            )
            .order_by(models.MailVerificationRequest.requested_at.asc())
            .limit(limit)
            .all()
        )
        live: list[models.MailVerificationRequest] = []
        for row in rows:
            expires_at = _aware(row.expires_at)
            if expires_at is not None and expires_at <= moment:
                row.status = models.MailVerificationRequest.EXPIRED
                row.last_reason = "expired before any matching message arrived"
                continue
            live.append(row)
        self.db.flush()
        return live

    # ------------------------------------------------------------------
    def examine(
        self,
        *,
        subject: str,
        body: str,
        sender: str,
        recipients: Iterable[str],
        received_at: datetime,
        message_id: Optional[str] = None,
    ) -> Examination:
        """Offer one arriving message to every live request this organisation is waiting on.

        Every live request is tried rather than the first, because an organisation may legitimately
        have two registrations in flight; each request re-applies the mailbox, sender and window
        checks, so trying more of them does not weaken the guarantee.
        """
        best = Examination(VerificationOutcome(VerificationKind.NONE, "no request was waiting"))
        matched_any = False

        for row in self.pending():
            requested_at = _aware(row.requested_at)
            expires_at = _aware(row.expires_at)
            anchor = VerificationRequest(
                org_id=row.org_id,
                address=row.address,
                portal_host=row.portal_host,
                purpose=row.purpose,
                requested_at=requested_at,
                ttl=(expires_at - requested_at),
            )
            outcome = extract(
                subject=subject, body=body, sender=sender, recipients=list(recipients or []),
                received_at=received_at, request=anchor,
            )
            # A message that reaches the mailbox check at all is one this request cares about, even
            # if it was not usable - that distinction is the operator's whole diagnosis.
            if "mailbox this verification was sent to" not in outcome.reason:
                matched_any = True
                row.examined = (row.examined or 0) + 1
                row.last_reason = outcome.reason[:500]
            elif not matched_any:
                best = Examination(outcome, request_id=row.id)

            if not outcome.usable:
                continue

            if self._satisfy(row, outcome, message_id=message_id):
                return Examination(outcome, request_id=row.id, satisfied=True)
            # Already satisfied by an identical code - a duplicate delivery, not an error.
            return Examination(outcome, request_id=row.id, already_satisfied=True)

        if not matched_any:
            best = Examination(
                VerificationOutcome(
                    VerificationKind.NONE,
                    "no live verification request matches this message",
                )
            )
        self.db.flush()
        return best

    # ------------------------------------------------------------------
    def _satisfy(
        self,
        row: models.MailVerificationRequest,
        outcome: VerificationOutcome,
        *,
        message_id: Optional[str],
    ) -> bool:
        """Mark satisfied, or detect that this exact result was already recorded.

        Returns True when this call did the satisfying. The uniqueness is enforced by the database
        and the IntegrityError is the signal: a pre-check would race two workers past each other.
        """
        row.fingerprint = outcome.fingerprint
        row.result_kind = outcome.kind.value
        row.result_code = outcome.code
        row.result_link = outcome.link
        row.satisfied_at = _now()
        row.satisfied_message_id = message_id
        row.status = models.MailVerificationRequest.SATISFIED
        row.last_reason = outcome.reason
        try:
            self.db.flush()
        except IntegrityError:
            # Another delivery (or another worker) recorded this identical result first. Roll back
            # to the savepoint and report it as already-satisfied: the caller must not apply the code
            # twice, and must not treat this as a failure either.
            self.db.rollback()
            logger.info(
                "verification_duplicate",
                extra={"org_id": self.org_id, "fingerprint": outcome.fingerprint[:80]},
            )
            return False
        logger.info(
            "verification_satisfied",
            extra={"org_id": self.org_id, "request_id": row.id, "kind": outcome.kind.value},
        )
        return True

    # ------------------------------------------------------------------
    def result_for(self, request_id: str) -> Optional[dict[str, Any]]:
        """What the waiting browser task needs: the code or link, or a statement that it is not
        ready. Never a bare None - "not yet" and "no such request" lead to different actions."""
        row = (
            self.db.query(models.MailVerificationRequest)
            .filter(
                models.MailVerificationRequest.id == request_id,
                models.MailVerificationRequest.org_id == self.org_id,
            )
            .first()
        )
        if row is None:
            return {"ready": False, "status": "UNKNOWN", "detail": "no such verification request"}
        if row.status == models.MailVerificationRequest.SATISFIED:
            return {
                "ready": True,
                "status": row.status,
                "kind": row.result_kind,
                "code": row.result_code,
                "link": row.result_link,
                "fingerprint": row.fingerprint,
            }
        if row.status == models.MailVerificationRequest.PENDING and (_aware(row.expires_at) or _now()) <= _now():
            row.status = models.MailVerificationRequest.EXPIRED
            row.last_reason = "expired before any matching message arrived"
            self.db.flush()
        return {
            "ready": False,
            "status": row.status,
            "detail": row.last_reason or "no matching message has arrived",
            "examined": row.examined or 0,
        }


def describe() -> dict[str, Any]:
    return {
        "ttl_default_seconds": int(DEFAULT_TTL.total_seconds()),
        "idempotency": "UNIQUE(org_id, fingerprint); IntegrityError is the duplicate signal",
        "anchors": "mailbox + portal host + time window, all three required",
    }
