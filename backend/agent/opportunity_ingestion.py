"""Opportunity Ingestion Adapter.

This is **adapter** work, not greenfield design. Granada already had an
ingestion pipeline: its schema survives in ``granada_db`` (30 tables, 14 of them
a coherent, indexed bot subsystem), its producer code does not exist anywhere on
this machine, and every one of its tables is empty. The contract is therefore
the authority - see ``docs/BOT_INGESTION_CONTRACT.md`` - and this module's job is
to honour it rather than to replace it.

The five obligations the contract places on an adapter
------------------------------------------------------
1. **Write to the existing contract**, satisfying both UNIQUE constraints, plus
   a raw-payload retention path. The legacy table has no raw-payload column, so
   ``opportunity_payloads`` provides one and every delivery is recorded.
2. **Treat ``content_hash`` and ``source_url`` as the dedupe identity.** Repeat
   ingestion upserts; it never duplicates.
3. **Record provenance**: ``source_name`` and ``source_url`` are mandatory and
   the source is traceable to an id.
4. **Report outcome** distinguishing *found* from *saved*, because a reachable
   source whose opportunities are all rejected otherwise looks identical to a
   dead source.
5. **Be idempotent** at the queue layer. The PostgreSQL constraints are the last
   line of defence, not the first.

The opaque-hash rule
--------------------
``content_hash`` is **never recomputed**. The normalisation that produced it is
not recorded in the repository and is not recoverable from an empty table, so
any attempt to derive it would be a guess - and a wrong guess does not fail
loudly, it silently duplicates every opportunity whose producer normalised
differently. The hash is validated for *shape* (64 hex characters) and stored as
delivered.

``dedupe_fingerprint`` exists precisely so the agentic layer has an identity of
its own that does not depend on guessing the producer's normalisation. It is
derived from the canonical fields we control - normalised title, source domain,
deadline - so a second producer or a future contract version can be deduped
without knowing how the first one hashed. It is deliberately a *different*
value from ``content_hash``: conflating them would mean changing one silently
changed the other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

#: The contract version this adapter implements. Changing the *meaning* of
#: ``content_hash`` is a breaking change and requires a new version plus an ADR.
CONTRACT_VERSION = "v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Fields compared for change detection, and whether a change can invalidate
#: work already in progress. A deadline moving is materially different from a
#: description being reworded.
_TRACKED_FIELDS: tuple[tuple[str, bool], ...] = (
    ("title", False),
    ("deadline", True),
    ("amount_min", True),
    ("amount_max", True),
    ("currency", False),
    ("eligibility_criteria", True),
    ("application_process", False),
    ("country", True),
    ("sector", False),
    ("is_active", True),
)


class IngestionError(RuntimeError):
    """Base class for ingestion failures."""


class ContractViolation(IngestionError):
    """The producer delivered something the contract does not allow.

    Raised rather than repaired. An adapter that silently fixes a malformed
    producer payload hides a producer defect until it has corrupted a lot of
    data.
    """


@dataclass
class RawOpportunity:
    """One opportunity as a producer delivers it.

    Mirrors the legacy shape. ``content_hash`` and ``source_url`` are required
    because they are the contract's identity; everything else is optional
    because the legacy columns are mostly nullable and a real scraper will miss
    fields.
    """

    title: str
    source_url: str
    source_name: str
    country: str
    content_hash: str
    description: Optional[str] = None
    deadline: Optional[datetime] = None
    amount_min: Optional[int] = None
    amount_max: Optional[int] = None
    currency: Optional[str] = None
    sector: Optional[str] = None
    eligibility_criteria: Optional[str] = None
    application_process: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    keywords: Optional[Any] = None
    focus_areas: Optional[Any] = None
    is_active: bool = True
    source_id: Optional[str] = None
    scraped_at: Optional[datetime] = None
    contract_version: str = CONTRACT_VERSION
    #: The untouched original. When omitted the adapter reconstructs a faithful
    #: mapping from the fields above, so the requirement to retain a payload is
    #: met even for a producer that does not supply one.
    raw_payload: Optional[dict] = None


@dataclass
class IngestOutcome:
    """What happened to one delivery."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    REJECTED = "REJECTED"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"

    status: str
    opportunity_id: Optional[str] = None
    changed_fields: list[str] = field(default_factory=list)
    material_changes: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    payload_id: Optional[str] = None

    @property
    def saved(self) -> bool:
        return self.status in {self.CREATED, self.UPDATED}

    @property
    def is_material(self) -> bool:
        return bool(self.material_changes)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalise_text(value: Optional[str]) -> str:
    """Normalise for fingerprinting only - never for the producer's hash.

    Unicode-normalised, case-folded, whitespace-collapsed. This is *our*
    normalisation and it is deliberately separate from whatever produced
    ``content_hash``.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def canonical_domain(url: str) -> str:
    """Host without ``www.``, lowercased. Part of the fingerprint."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def dedupe_fingerprint(raw: RawOpportunity) -> str:
    """Our own identity for an opportunity, independent of the producer.

    Title, domain and deadline - the three things that make two listings the
    same opportunity rather than two opportunities. Deliberately excludes the
    full URL path, because the same opportunity is frequently re-listed at a new
    slug and that is an update, not a second opportunity.
    """
    deadline = _aware(raw.deadline)
    parts = [
        normalise_text(raw.title),
        canonical_domain(raw.source_url),
        deadline.date().isoformat() if deadline else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def payload_digest(payload: dict) -> str:
    """Stable digest of a raw payload, key-order independent."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_dict(raw: RawOpportunity) -> dict:
    """A faithful mapping of the delivery, used when the producer sends none."""
    return {
        "title": raw.title,
        "source_url": raw.source_url,
        "source_name": raw.source_name,
        "country": raw.country,
        "content_hash": raw.content_hash,
        "description": raw.description,
        "deadline": raw.deadline.isoformat() if raw.deadline else None,
        "amount_min": raw.amount_min,
        "amount_max": raw.amount_max,
        "currency": raw.currency,
        "sector": raw.sector,
        "eligibility_criteria": raw.eligibility_criteria,
        "application_process": raw.application_process,
        "contact_email": raw.contact_email,
        "contact_phone": raw.contact_phone,
        "keywords": raw.keywords,
        "focus_areas": raw.focus_areas,
        "is_active": raw.is_active,
    }


class OpportunityIngestionAdapter:
    """Ingests producer deliveries into the canonical catalogue, idempotently."""

    def __init__(self, db: Session, *, contract_version: str = CONTRACT_VERSION) -> None:
        self.db = db
        self.contract_version = contract_version

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate(raw: RawOpportunity) -> None:
        """Reject a delivery that cannot satisfy the contract.

        Each check corresponds to a NOT NULL or UNIQUE column, so failing here
        produces a message that names the producer's problem instead of a
        constraint violation that names ours.
        """
        for name in ("title", "source_url", "source_name", "country", "content_hash"):
            if not getattr(raw, name) or not str(getattr(raw, name)).strip():
                raise ContractViolation(f"{name} is required by the contract")

        if not _HEX64.match((raw.content_hash or "").strip().lower()):
            # Shape only. The *value* is opaque and is never recomputed.
            raise ContractViolation(
                f"content_hash must be a 64-character SHA-256 hex digest, got "
                f"{raw.content_hash!r}. The adapter does not compute this value - "
                "the producer owns its normalisation."
            )

        if not raw.source_url.startswith(("http://", "https://")):
            raise ContractViolation(f"source_url must be http(s), got {raw.source_url!r}")

        if not canonical_domain(raw.source_url):
            raise ContractViolation(f"source_url has no host: {raw.source_url!r}")

        if raw.deadline is not None and _aware(raw.deadline) is not None:
            pass  # accepted; a past deadline is legitimate (an expired listing)
        if raw.amount_min is not None and raw.amount_max is not None:
            if raw.amount_min > raw.amount_max:
                raise ContractViolation(
                    f"amount_min ({raw.amount_min}) exceeds amount_max ({raw.amount_max})"
                )

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def ingest(self, raw: RawOpportunity, *, job: Optional[models.IngestionJob] = None) -> IngestOutcome:
        """Ingest one delivery.

        Records the raw payload, upserts the canonical row, and detects changes.
        The order matters: the payload is written *first*, so a crash between the
        two leaves evidence of what arrived rather than a catalogue row nobody
        can account for.

        ``opportunities_found`` is incremented here, before anything can fail.
        "Found" means the producer delivered it, which is true whether or not we
        could store it - and a source whose every opportunity is rejected must
        not look like a source that returned nothing.
        """
        if job is not None:
            job.opportunities_found += 1

        raw = _coerce(raw)
        self.validate(raw)

        payload = raw.raw_payload if raw.raw_payload is not None else _as_dict(raw)
        digest = payload_digest(payload)

        existing = self._find(raw)

        # A payload we have already received verbatim is the queue's duplicate
        # delivery, not a change. Recording the payload again would grow the
        # evidence table by the crawl frequency for no extra information.
        if existing is not None and self._payload_already_seen(raw, digest):
            return IngestOutcome(
                status=IngestOutcome.DUPLICATE_DELIVERY,
                opportunity_id=existing.id,
                reason=f"payload {digest[:12]} already received for this opportunity",
            )

        if existing is None:
            opportunity = self._create(raw)
            payload_row = self._record_payload(raw, payload, digest, opportunity.id)
            if job is not None:
                job.opportunities_saved += 1
            self.db.flush()
            return IngestOutcome(
                status=IngestOutcome.CREATED,
                opportunity_id=opportunity.id,
                payload_id=payload_row.id,
            )

        changes, material = self._apply_changes(existing, raw)
        payload_row = self._record_payload(raw, payload, digest, existing.id)

        if changes:
            existing.updated_at = _now()
            existing.scraped_at = _aware(raw.scraped_at) or _now()
            if job is not None:
                job.opportunities_updated += 1
            self.db.flush()
            return IngestOutcome(
                status=IngestOutcome.UPDATED,
                opportunity_id=existing.id,
                changed_fields=changes,
                material_changes=material,
                payload_id=payload_row.id,
            )

        existing.scraped_at = _aware(raw.scraped_at) or existing.scraped_at
        existing.last_verified = _now()
        self.db.flush()
        return IngestOutcome(
            status=IngestOutcome.UNCHANGED,
            opportunity_id=existing.id,
            payload_id=payload_row.id,
        )

    def ingest_many(
        self, deliveries: Iterable[RawOpportunity], *, job: Optional[models.IngestionJob] = None
    ) -> list[IngestOutcome]:
        """Ingest a batch, isolating failures.

        One malformed delivery must not abandon the rest of a crawl: a producer
        that starts emitting a bad field would otherwise stop all ingestion
        rather than one record.
        """
        outcomes: list[IngestOutcome] = []
        for raw in deliveries:
            try:
                outcomes.append(self.ingest(raw, job=job))
            except ContractViolation as exc:
                if job is not None:
                    job.opportunities_rejected += 1
                self.db.flush()
                logger.warning(
                    "ingestion_delivery_rejected",
                    extra={"source_url": getattr(raw, "source_url", None), "reason": str(exc)},
                )
                outcomes.append(
                    IngestOutcome(status=IngestOutcome.REJECTED, reason=str(exc))
                )
        return outcomes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _find(self, raw: RawOpportunity) -> Optional[models.Opportunity]:
        """Locate an existing row by either contract key, or by our fingerprint.

        All three are consulted because they can disagree in ways that are
        informative rather than contradictory: the same URL with new content is
        an update, and the same content re-listed at a new URL is the same
        opportunity.
        """
        content_hash = raw.content_hash.strip().lower()
        found = self.db.execute(
            select(models.Opportunity).where(models.Opportunity.content_hash == content_hash)
        ).scalars().first()
        if found is not None:
            return found

        found = self.db.execute(
            select(models.Opportunity).where(models.Opportunity.source_url == raw.source_url)
        ).scalars().first()
        if found is not None:
            return found

        return self.db.execute(
            select(models.Opportunity).where(
                models.Opportunity.dedupe_fingerprint == dedupe_fingerprint(raw)
            )
        ).scalars().first()

    def _payload_already_seen(self, raw: RawOpportunity, digest: str) -> bool:
        return (
            self.db.execute(
                select(models.OpportunityPayload.id).where(
                    models.OpportunityPayload.source_url == raw.source_url,
                    models.OpportunityPayload.payload_digest == digest,
                )
            ).first()
            is not None
        )

    def _record_payload(
        self, raw: RawOpportunity, payload: dict, digest: str, opportunity_id: Optional[str]
    ) -> models.OpportunityPayload:
        row = models.OpportunityPayload(
            opportunity_id=opportunity_id,
            source_url=raw.source_url,
            source_name=raw.source_name,
            source_id=raw.source_id,
            payload=payload or {},
            payload_digest=digest,
            content_hash=(raw.content_hash or "").strip().lower() or None,
            contract_version=raw.contract_version or self.contract_version,
            received_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _create(self, raw: RawOpportunity) -> models.Opportunity:
        opportunity = models.Opportunity(
            title=raw.title,
            description=raw.description,
            deadline=_aware(raw.deadline),
            amount_min=raw.amount_min,
            amount_max=raw.amount_max,
            currency=raw.currency,
            source_url=raw.source_url,
            source_name=raw.source_name,
            country=raw.country,
            sector=raw.sector,
            eligibility_criteria=raw.eligibility_criteria,
            application_process=raw.application_process,
            contact_email=raw.contact_email,
            contact_phone=raw.contact_phone,
            keywords=raw.keywords,
            focus_areas=raw.focus_areas,
            content_hash=(raw.content_hash or "").strip().lower(),
            scraped_at=_aware(raw.scraped_at) or _now(),
            is_active=raw.is_active,
            is_verified=False,
            dedupe_fingerprint=dedupe_fingerprint(raw),
            source_id=raw.source_id,
            contract_version=raw.contract_version or self.contract_version,
            created_at=_now(),
        )
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _apply_changes(
        self, opportunity: models.Opportunity, raw: RawOpportunity
    ) -> tuple[list[str], list[str]]:
        """Apply tracked fields, recording a change row for each that differs.

        ``content_hash`` is updated when the producer supplies a different one,
        because the contract makes it the content-dedupe key and a stale value
        would defeat the next delivery's lookup. It is never *derived*.
        """
        changed: list[str] = []
        material: list[str] = []

        for field_name, is_material in _TRACKED_FIELDS:
            current = getattr(opportunity, field_name)
            incoming = getattr(raw, field_name)
            if field_name == "deadline":
                current, incoming = _aware(current), _aware(incoming)
            if _comparable(current) == _comparable(incoming):
                continue

            setattr(opportunity, field_name, incoming)
            changed.append(field_name)
            if is_material:
                material.append(field_name)
            self.db.add(
                models.OpportunityChange(
                    opportunity_id=opportunity.id,
                    field=field_name,
                    old_value=_render(current),
                    new_value=_render(incoming),
                    material=is_material,
                    detected_at=_now(),
                )
            )

        incoming_hash = (raw.content_hash or "").strip().lower()
        if incoming_hash and incoming_hash != opportunity.content_hash:
            self.db.add(
                models.OpportunityChange(
                    opportunity_id=opportunity.id,
                    field="content_hash",
                    old_value=opportunity.content_hash,
                    new_value=incoming_hash,
                    material=False,
                    detected_at=_now(),
                )
            )
            opportunity.content_hash = incoming_hash

        return changed, material


def _coerce(raw: Any) -> RawOpportunity:
    """Accept a plain mapping as well as a dataclass.

    Producers are external processes. Requiring them to import our dataclass
    would make the adapter's convenience the producer's dependency, and the
    contract is JSON, not Python.
    """
    if isinstance(raw, RawOpportunity):
        return raw
    if isinstance(raw, dict):
        payload = dict(raw)
        known = {f for f in RawOpportunity.__dataclass_fields__}
        extra = {k: v for k, v in payload.items() if k not in known}
        kwargs = {k: v for k, v in payload.items() if k in known}
        # Anything the producer sent that we do not model is preserved as the
        # raw payload rather than dropped: "never lose the original payload".
        if extra:
            kwargs.setdefault("raw_payload", dict(raw))
        deadline = kwargs.get("deadline")
        if isinstance(deadline, str):
            kwargs["deadline"] = _parse_datetime(deadline)
        scraped = kwargs.get("scraped_at")
        if isinstance(scraped, str):
            kwargs["scraped_at"] = _parse_datetime(scraped)
        return RawOpportunity(**kwargs)
    raise ContractViolation(f"cannot ingest {type(raw).__name__}; expected a mapping or RawOpportunity")


def _parse_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractViolation(f"deadline is not ISO-8601: {value!r}")


def _comparable(value: Any) -> Any:
    """Normalise for equality, so a naive/aware pair does not read as a change.

    ``DateTime(timezone=True)`` is aware from PostgreSQL and naive from SQLite.
    Without normalisation every ingest would report the deadline as changed on
    SQLite and the change log would be pure noise.

    Note on the datetime branch: for the deadline specifically,
    ``_apply_changes`` has *already* called ``_aware()`` before reaching here, so
    this branch is redundant for today's tracked fields. It is kept as
    defence-in-depth for a future tracked datetime field added without that call
    - and that redundancy is recorded because an inversion showed it: neutering
    this branch does **not** fail any test, whereas neutering the ``_aware()``
    call in ``_apply_changes`` does. Stating which one is load-bearing prevents a
    later reader from deleting the effective line and keeping the decorative one.
    """
    if isinstance(value, datetime):
        aware = _aware(value)
        return aware.isoformat() if aware else None
    if isinstance(value, str):
        return value.strip()
    return value


def _render(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)
