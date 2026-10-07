"""Opportunity ingestion: adapter work against an existing producer contract.

The contract in ``docs/BOT_INGESTION_CONTRACT.md`` is authoritative because the
legacy bot subsystem's schema survives while its producer code does not. The
tests that matter most here are the compatibility ones: the two UNIQUE keys must
keep working, ``content_hash`` must stay opaque, and a repeat delivery must
upsert rather than duplicate. Losing any of those would destroy the only artifact
the existing pipeline left behind.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from agent.opportunity_ingestion import (  # noqa: E402
    CONTRACT_VERSION,
    ContractViolation,
    IngestOutcome,
    OpportunityIngestionAdapter,
    RawOpportunity,
    canonical_domain,
    dedupe_fingerprint,
    normalise_text,
    payload_digest,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalogue.db'}", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def adapter(db):
    return OpportunityIngestionAdapter(db)


def _hash(seed: str) -> str:
    """A producer-supplied hash. Deliberately arbitrary - it is opaque."""
    return hashlib.sha256(seed.encode()).hexdigest()


def _raw(**overrides) -> RawOpportunity:
    payload = {
        "title": "Community Health Grant 2027",
        "source_url": "https://funders.example.org/grants/health-2027",
        "source_name": "Example Funder",
        "country": "Uganda",
        "content_hash": _hash("seed-1"),
        "description": "Supports community health programmes.",
        "deadline": datetime(2027, 3, 31, tzinfo=timezone.utc),
        "amount_min": 10_000,
        "amount_max": 50_000,
        "currency": "USD",
        "sector": "Health",
    }
    payload.update(overrides)
    return RawOpportunity(**payload)


# ---------------------------------------------------------------------------
# Both contract keys must keep working
# ---------------------------------------------------------------------------
def test_ingest_creates_a_canonical_row(adapter, db):
    outcome = adapter.ingest(_raw())
    db.commit()

    assert outcome.status == IngestOutcome.CREATED
    row = db.execute(select(models.Opportunity)).scalar_one()
    assert row.title == "Community Health Grant 2027"
    assert row.source_url == "https://funders.example.org/grants/health-2027"
    assert row.content_hash == _hash("seed-1")
    assert row.contract_version == CONTRACT_VERSION


def test_a_repeat_delivery_upserts_rather_than_duplicating(adapter, db):
    """Obligation 2. The legacy system already deduplicated; so must this."""
    first = adapter.ingest(_raw())
    db.commit()
    second = adapter.ingest(_raw(source_url="https://funders.example.org/grants/health-2027"))
    db.commit()

    assert second.opportunity_id == first.opportunity_id
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1


def test_identical_payload_redelivery_is_reported_as_a_duplicate(adapter, db):
    """The queue is at-least-once, so the same delivery arrives twice."""
    adapter.ingest(_raw())
    db.commit()
    outcome = adapter.ingest(_raw())
    db.commit()

    assert outcome.status == IngestOutcome.DUPLICATE_DELIVERY
    assert outcome.saved is False
    # And it did not append another payload row: the evidence table must not grow
    # with the crawl frequency.
    assert len(db.execute(select(models.OpportunityPayload)).scalars().all()) == 1


def test_the_same_url_with_new_content_is_an_update(adapter, db):
    """``source_url`` is UNIQUE: the same URL is an update, not an insert."""
    adapter.ingest(_raw())
    db.commit()

    changed = adapter.ingest(_raw(title="Community Health Grant 2027 (revised)"))
    db.commit()

    assert changed.status == IngestOutcome.UPDATED
    assert "title" in changed.changed_fields
    rows = db.execute(select(models.Opportunity)).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Community Health Grant 2027 (revised)"


def test_the_same_opportunity_relisted_at_a_new_url_is_not_duplicated(adapter, db):
    """The fingerprint catches a re-listing the two producer keys cannot.

    The same opportunity is frequently re-published at a new slug with the same
    producer hash. Without the fingerprint that is two rows and a duplicate
    application.
    """
    adapter.ingest(_raw())
    db.commit()

    relisted = adapter.ingest(
        _raw(source_url="https://funders.example.org/opportunities/health-2027-relisted")
    )
    db.commit()

    assert relisted.opportunity_id is not None
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1


def test_the_content_hash_unique_key_actually_bites(db):
    """Proving the constraint exists rather than trusting the model declaration.

    A duplicate content_hash with a different URL must be rejected by the
    database. This is the last line of defence the contract names.
    """
    db.add(models.Opportunity(
        title="a", source_url="https://a.example.org/1", source_name="s", country="c",
        content_hash=_hash("shared"), dedupe_fingerprint=_hash("fp-1"), created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.add(models.Opportunity(
        title="b", source_url="https://b.example.org/2", source_name="s", country="c",
        content_hash=_hash("shared"), dedupe_fingerprint=_hash("fp-2"), created_at=datetime.now(timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_the_source_url_unique_key_actually_bites(db):
    db.add(models.Opportunity(
        title="a", source_url="https://same.example.org/x", source_name="s", country="c",
        content_hash=_hash("h1"), dedupe_fingerprint=_hash("f1"), created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.add(models.Opportunity(
        title="b", source_url="https://same.example.org/x", source_name="s", country="c",
        content_hash=_hash("h2"), dedupe_fingerprint=_hash("f2"), created_at=datetime.now(timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# The hash is opaque
# ---------------------------------------------------------------------------
def test_the_content_hash_is_stored_as_delivered_and_never_recomputed(adapter, db):
    """A deliberately weird hash must survive verbatim.

    The normalisation that produced ``content_hash`` is not recoverable, so any
    adapter that recomputed it would silently duplicate every opportunity whose
    producer normalised differently. This asserts the value is *passed through*.
    """
    arbitrary = _hash("something-only-the-producer-knows")
    adapter.ingest(_raw(content_hash=arbitrary))
    db.commit()
    row = db.execute(select(models.Opportunity)).scalar_one()
    assert row.content_hash == arbitrary
    assert row.content_hash != dedupe_fingerprint(_raw())


def test_a_hash_that_is_not_sha256_shaped_is_rejected(adapter, db):
    """Shape is validated; the value is not. A wrong shape is a producer bug."""
    with pytest.raises(ContractViolation) as excinfo:
        adapter.ingest(_raw(content_hash="not-a-hash"))
    assert "producer owns its normalisation" in str(excinfo.value)

    with pytest.raises(ContractViolation):
        adapter.ingest(_raw(content_hash="abc"))
    # 64 characters but not hex.
    with pytest.raises(ContractViolation):
        adapter.ingest(_raw(content_hash="z" * 64))


def test_the_fingerprint_is_independent_of_the_producer_hash():
    """Otherwise changing one would silently change the other."""
    a = dedupe_fingerprint(_raw(content_hash=_hash("one")))
    b = dedupe_fingerprint(_raw(content_hash=_hash("two")))
    assert a == b, "the fingerprint depends on the producer's opaque hash"


def test_the_fingerprint_ignores_the_url_path_but_not_the_domain():
    """A re-listing at a new slug is the same opportunity; another funder's site
    is not."""
    base = dedupe_fingerprint(_raw())
    relisted = dedupe_fingerprint(
        _raw(source_url="https://funders.example.org/opportunities/other-slug")
    )
    other_funder = dedupe_fingerprint(
        _raw(source_url="https://other.example.com/grants/health-2027")
    )
    assert base == relisted
    assert base != other_funder


def test_the_fingerprint_ignores_case_and_padding_in_the_title():
    a = dedupe_fingerprint(_raw(title="Community  Health   Grant 2027"))
    b = dedupe_fingerprint(_raw(title="community health grant 2027"))
    assert a == b


def test_identical_title_and_deadline_at_one_funder_is_one_opportunity(adapter, db):
    """The fingerprint's aggressive case, stated explicitly.

    Two deliveries that agree on title, domain **and** deadline at the same
    funder are treated as one opportunity, and the second URL does not create a
    second row. That is a deliberate trade: a duplicate application is worse than
    a missed re-listing, and this is the strongest available signal that they are
    the same thing.

    The cost is real and worth naming: two genuinely different grants that happen
    to share an exact title and deadline at one funder would collapse into one.
    The fingerprint can be tightened without touching the two producer keys, but
    it must not be *loosened* casually, because the alternative failure -
    applying twice to the same grant - is the one the platform is judged on.
    """
    first = adapter.ingest(_raw())
    db.commit()
    second = adapter.ingest(_raw(source_url="https://funders.example.org/different-slug"))
    db.commit()

    assert second.opportunity_id == first.opportunity_id
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1


def test_the_content_hash_key_works_when_it_is_the_only_clue(adapter, db):
    """Each of the three dedupe keys must be load-bearing ON ITS OWN.

    The first version of this suite proved none of them: every fixture made all
    three keys agree, so neutering any one lookup still found the row through the
    others. Three redundant keys and no proof that any of them works is exactly
    the failure mode the inversion harness exists to catch.

    Here the producer's own hash is the **only** signal: new URL, new title, new
    deadline, so neither ``source_url`` nor the fingerprint can match.
    """
    adapter.ingest(_raw())
    db.commit()

    seen_again = adapter.ingest(_raw(
        title="A completely different title",
        source_url="https://funders.example.org/moved-here",
        deadline=datetime(2029, 12, 31, tzinfo=timezone.utc),
    ))  # same content_hash
    db.commit()

    assert seen_again.opportunity_id is not None
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1, (
        "the content_hash key did not dedupe"
    )


def test_the_source_url_key_works_when_it_is_the_only_clue(adapter, db):
    """Same URL, but a new producer hash AND a new title and deadline.

    The contract is explicit: the same opportunity re-listed at the same URL is
    an update, not an insert. Only ``source_url`` can establish that here.
    """
    adapter.ingest(_raw())
    db.commit()

    updated = adapter.ingest(_raw(
        title="Different title",
        content_hash=_hash("brand-new-producer-hash"),
        deadline=datetime(2030, 1, 1, tzinfo=timezone.utc),
    ))  # same source_url
    db.commit()

    assert updated.status == IngestOutcome.UPDATED
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1, (
        "the source_url key did not dedupe"
    )


def test_the_fingerprint_works_when_it_is_the_only_clue(adapter, db):
    """New URL and new producer hash, but the same title, domain and deadline.

    This is the re-listing case the two producer keys cannot see, and the reason
    the fingerprint exists at all.
    """
    adapter.ingest(_raw())
    db.commit()

    relisted = adapter.ingest(_raw(
        source_url="https://funders.example.org/new-slug",
        content_hash=_hash("a-different-producer-hash"),
    ))  # same title, domain and deadline
    db.commit()

    assert relisted.opportunity_id is not None
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 1, (
        "the fingerprint did not dedupe"
    )


def test_a_different_deadline_at_the_same_funder_is_a_different_opportunity(adapter, db):
    """The control case for the test above: the fingerprint must not collapse
    everything from one source."""
    adapter.ingest(_raw())
    db.commit()
    adapter.ingest(_raw(
        title="Water and Sanitation Grant 2027",
        source_url="https://funders.example.org/water-2027",
        content_hash=_hash("water"),
        deadline=datetime(2027, 9, 30, tzinfo=timezone.utc),
    ))
    db.commit()
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 2


def test_canonical_domain_strips_www():
    assert canonical_domain("https://www.Example.ORG/a/b") == "example.org"
    assert canonical_domain("https://example.org/a") == "example.org"


def test_normalise_text_is_unicode_safe():
    assert normalise_text("Café  GRANT") == "café grant"
    assert normalise_text(None) == ""
    assert normalise_text("  a   b  ") == "a b"


# ---------------------------------------------------------------------------
# Raw payload retention: "never lose the original payload"
# ---------------------------------------------------------------------------
def test_the_raw_payload_is_retained_verbatim(adapter, db):
    """The legacy table had nowhere to put one. This is the requirement that
    cannot be met by ``donor_opportunities`` alone."""
    original = {"html": "<html>raw</html>", "jsonld": {"@type": "Grant"}, "weird": [1, 2]}
    adapter.ingest(_raw(raw_payload=original))
    db.commit()

    row = db.execute(select(models.OpportunityPayload)).scalar_one()
    assert row.payload == original
    assert row.payload_digest == payload_digest(original)


def test_every_delivery_is_recorded_even_when_nothing_changed(adapter, db):
    """The question the table answers is "what did the producer actually say".

    A producer that suddenly starts omitting a field is only visible by keeping
    the deliveries that changed nothing.
    """
    adapter.ingest(_raw(raw_payload={"v": 1}))
    db.commit()
    adapter.ingest(_raw(raw_payload={"v": 2}))
    db.commit()
    adapter.ingest(_raw(raw_payload={"v": 3}))
    db.commit()

    payloads = db.execute(
        select(models.OpportunityPayload).order_by(models.OpportunityPayload.received_at)
    ).scalars().all()
    assert [p.payload["v"] for p in payloads] == [1, 2, 3]


def test_unmodelled_producer_fields_are_preserved_in_the_payload(adapter, db):
    """A field we do not model must not be dropped on the floor."""
    adapter.ingest({
        "title": "T", "source_url": "https://x.example.org/1", "source_name": "S",
        "country": "UG", "content_hash": _hash("z"), "unknown_producer_field": "keep me",
    })
    db.commit()
    row = db.execute(select(models.OpportunityPayload)).scalar_one()
    assert row.payload["unknown_producer_field"] == "keep me"


def test_a_payload_with_no_opportunity_id_is_storable():
    """The payload column is NOT NULL by design: a row that lost the payload
    must not be storable."""
    assert models.OpportunityPayload.__table__.c.payload.nullable is False


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def test_provenance_is_required(adapter):
    for field in ("title", "source_url", "source_name", "country", "content_hash"):
        with pytest.raises(ContractViolation):
            adapter.ingest(_raw(**{field: ""}))


def test_a_non_http_source_url_is_rejected(adapter):
    with pytest.raises(ContractViolation):
        adapter.ingest(_raw(source_url="ftp://example.org/x"))


def test_the_source_id_is_recorded_for_traceability(adapter, db):
    """The contract requires the source be traceable to a ``funding_sources`` row."""
    source_id = str(uuid.uuid4())
    adapter.ingest(_raw(source_id=source_id))
    db.commit()
    assert db.execute(select(models.Opportunity)).scalar_one().source_id == source_id


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------
def test_a_deadline_move_is_recorded_as_material(adapter, db):
    """A deadline move can invalidate an application already in preparation.
    A description being reworded cannot."""
    adapter.ingest(_raw())
    db.commit()
    new_deadline = datetime(2027, 6, 30, tzinfo=timezone.utc)
    outcome = adapter.ingest(_raw(deadline=new_deadline))
    db.commit()

    assert outcome.status == IngestOutcome.UPDATED
    assert "deadline" in outcome.material_changes
    assert outcome.is_material is True

    change = db.execute(select(models.OpportunityChange)).scalar_one()
    assert change.field == "deadline"
    assert change.material is True
    assert "2027-03-31" in change.old_value
    assert "2027-06-30" in change.new_value


def test_a_title_reword_is_recorded_but_not_material(adapter, db):
    adapter.ingest(_raw())
    db.commit()
    outcome = adapter.ingest(_raw(title="Community Health Grant 2027 - now open"))
    db.commit()

    assert "title" in outcome.changed_fields
    assert outcome.material_changes == []
    assert outcome.is_material is False
    assert db.execute(select(models.OpportunityChange)).scalar_one().material is False


def test_a_change_of_eligibility_is_material(adapter, db):
    """Eligibility is the field that decides whether it was ever worth applying."""
    adapter.ingest(_raw())
    db.commit()
    outcome = adapter.ingest(_raw(eligibility_criteria="Registered NGOs only"))
    db.commit()
    assert "eligibility_criteria" in outcome.material_changes


def test_material_changes_are_queryable_for_alerting(adapter, db):
    """The brief requires change detection *alerting*, so the material ones must
    be retrievable without scanning the whole log."""
    adapter.ingest(_raw())
    db.commit()
    adapter.ingest(_raw(title="reworded"))
    db.commit()
    adapter.ingest(_raw(deadline=datetime(2028, 1, 1, tzinfo=timezone.utc)))
    db.commit()

    material = db.execute(
        select(models.OpportunityChange).where(models.OpportunityChange.material.is_(True))
    ).scalars().all()
    assert [c.field for c in material] == ["deadline"]


def test_an_unchanged_delivery_records_no_change(adapter, db):
    adapter.ingest(_raw(raw_payload={"v": 1}))
    db.commit()
    adapter.ingest(_raw(raw_payload={"v": 2}))
    db.commit()
    assert db.execute(select(models.OpportunityChange)).scalars().all() == []


def test_a_deadline_is_not_reported_as_changed_when_only_its_awareness_differs(adapter, db):
    """``DateTime(timezone=True)`` is aware from PostgreSQL and naive from SQLite.

    **This test was vacuous when first written, and the inversion harness is what
    showed it.** It round-tripped a deadline through SQLite and re-delivered it,
    but SQLite returns naive datetimes, so both sides were naive, they compared
    equal without any normalisation, and deleting *both* normalisation layers
    still passed.

    On SQLite the naive/aware mismatch therefore cannot arise through the
    integration path at all, so it is asserted where it genuinely lives: on the
    comparison helper, with a hand-built aware/naive pair. Deleting the helper's
    normalisation is then a real failure.
    """
    from agent.opportunity_ingestion import _comparable

    aware = datetime(2027, 3, 31, 12, 0, tzinfo=timezone.utc)
    naive_same_instant = datetime(2027, 3, 31, 12, 0)

    assert _comparable(aware) == _comparable(naive_same_instant), (
        "an aware and a naive timestamp for the same instant compared as different, "
        "which would make every ingest report the deadline as changed on one backend"
    )
    # The control: genuinely different instants must still compare differently,
    # or the helper would be suppressing real changes.
    assert _comparable(aware) != _comparable(datetime(2027, 4, 1, 12, 0))

    # And the integration path still works for the ordinary case.
    adapter.ingest(_raw())
    db.commit()
    outcome = adapter.ingest(_raw(deadline=datetime(2027, 3, 31, tzinfo=timezone.utc)))
    db.commit()
    assert "deadline" not in outcome.changed_fields


def test_a_different_content_hash_is_recorded_as_a_change(adapter, db):
    """The contract makes it the dedupe key, so a stale value defeats the next
    lookup."""
    adapter.ingest(_raw())
    db.commit()
    adapter.ingest(_raw(content_hash=_hash("seed-2"), title="Different title entirely"))
    db.commit()

    fields = {c.field for c in db.execute(select(models.OpportunityChange)).scalars()}
    assert "content_hash" in fields


# ---------------------------------------------------------------------------
# Run outcome reporting: found vs saved
# ---------------------------------------------------------------------------
def test_an_ingestion_job_distinguishes_found_from_saved(adapter, db):
    """A reachable source whose opportunities are all rejected otherwise looks
    identical to a dead source."""
    job = models.IngestionJob(
        source_name="Example Funder", status=models.IngestionJob.RUNNING,
        started_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    adapter.ingest_many(
        [
            _raw(),
            # Genuinely a second opportunity: the fingerprint is
            # (title, domain, deadline), so an identical title AND deadline at
            # the same funder really is the same opportunity. The first version
            # of this test varied only the URL and was wrong to expect two rows.
            _raw(title="Water and Sanitation Grant 2027",
                 source_url="https://funders.example.org/2",
                 content_hash=_hash("b"),
                 deadline=datetime(2027, 9, 30, tzinfo=timezone.utc)),
            {"title": "", "source_url": "https://x.example.org/1", "source_name": "S",
             "country": "UG", "content_hash": _hash("c")},
        ],
        job=job,
    )
    db.commit()

    assert job.opportunities_found == 3
    assert job.opportunities_saved == 2
    assert job.opportunities_rejected == 1


def test_one_bad_delivery_does_not_abandon_the_batch(adapter, db):
    """A producer that starts emitting a bad field must not stop all ingestion."""
    outcomes = adapter.ingest_many([
        _raw(),
        _raw(source_url="https://bad.example.org/x", content_hash="nope"),
        _raw(source_url="https://funders.example.org/3", content_hash=_hash("c"),
             title="Third opportunity"),
    ])
    db.commit()

    assert [o.status for o in outcomes] == [
        IngestOutcome.CREATED, IngestOutcome.REJECTED, IngestOutcome.CREATED
    ]
    assert len(db.execute(select(models.Opportunity)).scalars().all()) == 2


def test_an_update_is_counted_separately_from_a_save(adapter, db):
    """The counters must not silently change meaning.

    ``opportunities_saved`` is meant to be "new rows stored". If an update also
    incremented it, a source that only ever re-lists old opportunities would look
    productive.
    """
    job = models.IngestionJob(
        source_name="Example Funder", status=models.IngestionJob.RUNNING,
        started_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    adapter.ingest(_raw(), job=job)
    db.commit()
    saved_after_create = job.opportunities_saved

    adapter.ingest(_raw(title="Reworded title"), job=job)
    db.commit()

    assert job.opportunities_updated == 1
    assert job.opportunities_saved == saved_after_create, (
        "an update incremented the saved counter"
    )


# ---------------------------------------------------------------------------
# Arithmetic sanity
# ---------------------------------------------------------------------------
def test_amount_min_above_amount_max_is_rejected(adapter):
    """A producer bug that would otherwise poison matching."""
    with pytest.raises(ContractViolation) as excinfo:
        adapter.ingest(_raw(amount_min=50_000, amount_max=10_000))
    assert "exceeds" in str(excinfo.value)


def test_a_past_deadline_is_accepted(adapter, db):
    """An expired listing is legitimate data, not a contract violation.

    Rejecting it would make the adapter unable to ingest a funder's archive, and
    the deadline scan needs to see that it lapsed.
    """
    adapter.ingest(_raw(deadline=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    assert db.execute(select(models.Opportunity)).scalar_one().deadline is not None


def test_a_plain_mapping_is_accepted_as_well_as_a_dataclass(adapter, db):
    """Producers are external processes; the contract is JSON, not Python."""
    outcome = adapter.ingest({
        "title": "Mapping delivery", "source_url": "https://m.example.org/1",
        "source_name": "M", "country": "KE", "content_hash": _hash("m"),
        "deadline": "2027-01-01T00:00:00Z",
    })
    db.commit()
    assert outcome.status == IngestOutcome.CREATED
    assert db.execute(select(models.Opportunity)).scalar_one().deadline is not None


def test_a_bad_iso_datetime_is_a_contract_violation(adapter):
    with pytest.raises(ContractViolation):
        adapter.ingest({
            "title": "T", "source_url": "https://m.example.org/2", "source_name": "M",
            "country": "KE", "content_hash": _hash("m"), "deadline": "next Tuesday",
        })


def test_an_unsupported_type_is_refused(adapter):
    with pytest.raises(ContractViolation):
        adapter.ingest(42)
