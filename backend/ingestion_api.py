"""The HTTP doorway for producers. The piece that made a crawler impossible.

WHY THIS EXISTS
---------------
Granada's catalogue has an ingestion ADAPTER (`agent/opportunity_ingestion.py`) but had **no way in
over HTTP** - I checked `main.py`, `router.py` and `agent_api.py` and nothing accepted an opportunity.
The only route was direct database access, which means every crawler needs database credentials. For
a design whose entire premise is that crawlers are *separate systems feeding one shared catalogue*,
that is the wrong shape: it hands out the keys to the tenant database so that a process can do the
one thing that must be done unscoped.

This endpoint is authenticated by a **bot key**, not a database password, and it reaches the
catalogue through the same validated adapter the tests exercise - so the contract, the dedupe and the
raw-payload retention all apply identically whether a delivery arrives by HTTP or by script.

THE FOUR DECISIONS WORTH KNOWING
--------------------------------
**1. The write is deliberately UNSCOPED.** ADR-0009 D2: the funding catalogue is shared, and
`opportunities_insert` requires `app.current_org() IS NULL`. A tenant cannot write to the catalogue,
by design - so this endpoint must NOT bind a tenant. That is the opposite of every other write in
this codebase, and it is the point rather than an oversight.

**2. A bad delivery must not abandon the crawl.** `ingest_many` isolates per-delivery failures, and
this endpoint keeps that: one malformed record returns `REJECTED` while the rest of the batch
ingests. A producer that emits one bad field should lose one opportunity, not an entire run.

**3. The key comparison is timing-safe.** `secrets.compare_digest`, not `==`. A byte-by-byte
comparison leaks the key's prefix to anyone who can measure response times, and an ingestion key is
worth attacking: it writes to the shared catalogue every organisation reads.

**4. The batch and the fields are bounded.** An unauthenticated-looking endpoint that accepts an
unbounded array is a memory-exhaustion vector regardless of who is allowed to call it.

WHAT THIS DELIBERATELY DOES NOT DO YET
--------------------------------------
One shared key in configuration, not a per-bot registry. The contract's `search_bots` table implies
per-bot identity, revocation and audit - which needs a migration. That is the honest next step, and
it is stated here rather than implied: today all producers share one credential, so revoking one
means revoking all, and the audit can say *what* arrived but not reliably *which bot* sent it.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from agent.opportunity_ingestion import (
    ContractViolation,
    OpportunityIngestionAdapter,
    RawOpportunity,
)
from config import settings
from database import get_db
from observability import metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["Ingestion"])

#: The largest batch accepted. A crawler delivering a whole crawl in one request is not how this is
#: meant to be used - batches keep the transaction short and a failure cheap.
MAX_BATCH = 200

#: Bounds on free text. The database has its own limits, but refusing early gives the producer a
#: useful message instead of a driver error, and stops an oversized body reaching the adapter.
MAX_TITLE = 500
MAX_DESCRIPTION = 20_000
MAX_SHORT = 500


class DeliveryIn(BaseModel):
    """One producer delivery, in the shape `docs/BOT_INGESTION_CONTRACT.md` specifies."""

    title: str = Field(..., max_length=MAX_TITLE)
    source_url: str = Field(..., max_length=MAX_SHORT)
    source_name: str = Field(..., max_length=MAX_SHORT)
    country: str = Field(..., max_length=100)
    content_hash: str = Field(..., max_length=64, min_length=64)
    description: Optional[str] = Field(None, max_length=MAX_DESCRIPTION)
    deadline: Optional[datetime] = None
    amount_min: Optional[int] = None
    amount_max: Optional[int] = None
    currency: Optional[str] = Field(None, max_length=8)
    sector: Optional[str] = Field(None, max_length=120)
    eligibility_criteria: Optional[str] = Field(None, max_length=MAX_DESCRIPTION)
    application_process: Optional[str] = Field(None, max_length=MAX_DESCRIPTION)
    contact_email: Optional[str] = Field(None, max_length=320)
    contact_phone: Optional[str] = Field(None, max_length=64)
    keywords: Optional[Any] = None
    focus_areas: Optional[Any] = None
    is_active: bool = True
    source_id: Optional[str] = Field(None, max_length=120)
    scraped_at: Optional[datetime] = None
    contract_version: str = Field("v1", max_length=20)
    raw_payload: Optional[dict] = None

    @field_validator("content_hash")
    @classmethod
    def _hash_is_a_sha256_digest(cls, value: str) -> str:
        """Checked HERE as well as in the adapter, so a producer gets a 422 with a sentence.

        The adapter's refusal is the authority and is tested; this is about the error a caller sees.
        The adapter does not compute normalisation because the producer owns it - and a delivery that
        gets it wrong should be told which field rather than discovering it in a per-item REJECTED.
        """
        cleaned = (value or "").strip().lower()
        if len(cleaned) != 64 or any(c not in "0123456789abcdef" for c in cleaned):
            raise ValueError(
                "content_hash must be a 64-character SHA-256 hex digest. The adapter does not "
                "compute this - the producer owns its normalisation."
            )
        return cleaned

    def to_raw(self) -> RawOpportunity:
        return RawOpportunity(
            title=self.title,
            source_url=self.source_url,
            source_name=self.source_name,
            country=self.country,
            content_hash=self.content_hash,
            description=self.description,
            deadline=self.deadline,
            amount_min=self.amount_min,
            amount_max=self.amount_max,
            currency=self.currency,
            sector=self.sector,
            eligibility_criteria=self.eligibility_criteria,
            application_process=self.application_process,
            contact_email=self.contact_email,
            contact_phone=self.contact_phone,
            keywords=self.keywords,
            focus_areas=self.focus_areas,
            is_active=self.is_active,
            source_id=self.source_id,
            scraped_at=self.scraped_at,
            contract_version=self.contract_version,
            raw_payload=self.raw_payload,
        )


class BatchIn(BaseModel):
    deliveries: list[DeliveryIn] = Field(..., min_length=1, max_length=MAX_BATCH)


class OutcomeOut(BaseModel):
    status: str
    opportunity_id: Optional[str] = None
    reason: Optional[str] = None
    changed_fields: list[str] = Field(default_factory=list)


class BatchOut(BaseModel):
    received: int
    created: int
    updated: int
    unchanged: int
    #: Malformed deliveries. A problem.
    rejected: int
    #: Deliveries the dedupe identity already knew. Not a problem - the reason a crawl is re-runnable.
    duplicate: int = 0
    outcomes: list[OutcomeOut]


def require_bot_key(x_bot_key: str = Header(default="", alias="X-Bot-Key")) -> str:
    """The producer credential.

    Timing-safe comparison, and a refusal when no key is configured at all: an endpoint that writes
    to the shared catalogue must be closed by default, not open because somebody forgot to set a
    variable.
    """
    configured = (getattr(settings, "ingest_bot_key", "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ingestion is not configured on this deployment: set INGEST_BOT_KEY to enable it. "
                "Refusing rather than accepting unauthenticated writes to the shared catalogue."
            ),
        )
    if not x_bot_key or not secrets.compare_digest(x_bot_key.strip(), configured):
        # One message for both cases. Distinguishing "missing" from "wrong" tells an attacker which
        # half of the problem they have solved.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid X-Bot-Key header is required",
        )
    return "bot"


@router.post(
    "/opportunities",
    response_model=BatchOut,
    status_code=status.HTTP_200_OK,
    summary="Deliver opportunities from a producer",
)
def ingest_opportunities(
    payload: BatchIn,
    request: Request,
    db: Session = Depends(get_db),
    _bot: str = Depends(require_bot_key),
) -> BatchOut:
    """Ingest a batch of producer deliveries into the shared catalogue.

    Idempotent by contract: `content_hash` and `source_url` are the dedupe identity, so a producer
    that re-sends yesterday's crawl updates rather than duplicating. A crawl retry must never create
    a second copy of an opportunity, because every organisation reads this table.
    """
    adapter = OpportunityIngestionAdapter(db)
    outcomes = adapter.ingest_many([delivery.to_raw() for delivery in payload.deliveries])
    db.commit()

    counted = {"CREATED": 0, "UPDATED": 0, "UNCHANGED": 0, "REJECTED": 0, "DUPLICATE_DELIVERY": 0}
    response: list[OutcomeOut] = []
    for outcome in outcomes:
        counted[outcome.status] = counted.get(outcome.status, 0) + 1
        response.append(
            OutcomeOut(
                status=outcome.status,
                opportunity_id=outcome.opportunity_id,
                reason=outcome.reason,
                changed_fields=list(outcome.changed_fields or []),
            )
        )

    # TWO OPPOSITE FACTS, REPORTED SEPARATELY.
    #
    #   REJECTED           - a malformed delivery. A real problem somebody must fix.
    #   DUPLICATE_DELIVERY - the dedupe identity working. A SUCCESS, and the reason a producer can
    #                        re-run a crawl without fear.
    #
    # Summed into one `rejected` field they read as "forty-nine things broke" when forty-nine things
    # were in fact recognised as already-delivered. `rejected` now means only the first.
    rejected = counted["REJECTED"]
    duplicate = counted["DUPLICATE_DELIVERY"]
    # THE KEYS ARE NOT `created` / `updated`, and that is not a style choice.
    #
    # `LogRecord` already has a `created` attribute (the timestamp), and `Logger.makeRecord` raises
    # `KeyError: "Attempt to overwrite 'created' in LogRecord"` when `extra` collides with one. That
    # made this endpoint return **500 Internal Server Error** on the first real producer delivery,
    # after the batch had already been ingested - so the data landed and the caller was told it had
    # failed.
    #
    # The test suite could not catch it: `Logger._log` checks the level BEFORE building the record,
    # so with pytest's default WARNING root level this line was a no-op in every test and only ran
    # once INFO logging was enabled in production. `test_ingestion_api.py` now enables INFO
    # explicitly, which is what pins it.
    logger.info(
        "ingest.batch",
        extra={
            "received_count": len(payload.deliveries),
            "created_count": counted["CREATED"],
            "updated_count": counted["UPDATED"],
            "rejected_count": rejected,
            "duplicate_count": duplicate,
        },
    )
    try:  # pragma: no cover - metrics are best-effort by design
        metrics.counter("ingest_deliveries_total", len(payload.deliveries))
    except Exception:  # noqa: BLE001
        pass

    return BatchOut(
        received=len(payload.deliveries),
        created=counted["CREATED"],
        updated=counted["UPDATED"],
        unchanged=counted["UNCHANGED"],
        rejected=rejected,
        duplicate=duplicate,
        outcomes=response,
    )


@router.get("/contract", summary="What a producer must send")
def describe_contract() -> dict[str, Any]:
    """The contract, in the API rather than only in a document.

    A producer author should not have to read a markdown file to discover that `content_hash` must be
    a SHA-256 digest the producer computes itself. Public on purpose: the schema is not a secret, and
    the endpoint that ACCEPTS deliveries is the one that needs a key.
    """
    return {
        "contract_version": "v1",
        "endpoint": "POST /api/v1/ingest/opportunities",
        "auth": "X-Bot-Key header",
        "batch_limit": MAX_BATCH,
        "dedupe_identity": ["content_hash", "source_url"],
        "content_hash": (
            "SHA-256 hex digest of the producer's canonicalised payload. Granada does NOT compute "
            "this: the producer owns its normalisation, so two producers that normalise differently "
            "are two different opportunities rather than one corrupted one."
        ),
        "required_fields": ["title", "source_url", "source_name", "country", "content_hash"],
        "outcomes": {
            "CREATED": "new opportunity",
            "UPDATED": "the same identity arrived with material changes",
            "UNCHANGED": "the same identity arrived byte-identical; a re-run",
            "REJECTED": "malformed; the rest of the batch continues",
            "DUPLICATE_DELIVERY": "the same delivery id was seen before",
        },
    }
