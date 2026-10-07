"""Eligibility gates and ranking.

The load-bearing test is ``test_a_high_semantic_score_never_overrides_a_hard_gate``.
It is the brief's rule and the failure that makes an autonomous matching engine
dangerous: a confident score for an opportunity the organisation is legally
ineligible for, which something downstream then trusts.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.matching import (  # noqa: E402
    FAIL,
    PASS,
    UNKNOWN,
    EligibilityEngine,
    Matcher,
    MatchingError,
)
from agent.organisation_memory import DocumentVault, OrganisationMemory, checksum_bytes  # noqa: E402


@pytest.fixture
def db(tmp_path):
    # Schema copied from a session template rather than rebuilt: create_all to a
    # file on this filesystem costs ~3.8s per test because the schema has 38 tables
    # and 203 indexes. See tests/conftest.py::make_sqlite_db.
    engine, session = make_sqlite_db(tmp_path, "matching.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Uganda Health NGO", slug="ug-health", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id


def _fact(db, org, key, value, *, state=models.OrgFact.VERIFIED, valid_until=None, source="user:1"):
    OrganisationMemory(db, org).record_fact(
        key=key, value=value, state=state, source=source, valid_until=valid_until
    )
    db.commit()


def _opportunity(db, **overrides):
    payload = {
        "title": "Community Health Grant",
        "source_url": f"https://funders.example.org/{uuid.uuid4().hex[:8]}",
        "source_name": "Example Funder",
        "country": "Uganda",
        "content_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "dedupe_fingerprint": uuid.uuid4().hex + uuid.uuid4().hex,
        "is_active": True,
        "deadline": datetime.now(timezone.utc) + timedelta(days=30),
        "amount_max": 50_000,
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    row = models.Opportunity(**payload)
    db.add(row)
    db.commit()
    return row


def _ready_org(db, org):
    """An organisation that passes every gate, so one change can be isolated."""
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_valid_until", "2030-01-01",
          valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc))
    return org


# ---------------------------------------------------------------------------
# THE rule
# ---------------------------------------------------------------------------
def test_a_high_semantic_score_never_overrides_a_hard_gate(db, org):
    """A confident score for an ineligible opportunity must not exist at all.

    The guarantee is structural: the scorer is only *called* for opportunities
    that already passed every hard gate. So there is no score to override the gate
    with - which is stronger than "we remember to ignore the score", because a
    forgotten guard leaves a confident number in the database for a later reader
    to trust.
    """
    _fact(db, org, "country", "Kenya")  # NOT Uganda
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_valid_until", "2030-01-01",
          valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc))
    opportunity = _opportunity(db, country="Uganda")

    calls: list[str] = []

    def confident_scorer(candidate):
        calls.append(candidate.id)
        return 0.99  # a very strong score for an ineligible opportunity

    matcher = Matcher(db, org, semantic_scorer=confident_scorer, scorer_name="stub")
    matches = matcher.evaluate([opportunity])
    db.commit()

    match = matches[0]
    assert match.qualification.state == models.OpportunityMatch.REJECTED_BY_RULE
    assert "country_eligible" in match.qualification.failed_gates
    # No score, because none was computed.
    assert match.semantic_score is None
    assert match.final_score is None
    assert match.rank is None
    assert calls == [], "the semantic scorer was called for an ineligible opportunity"
    assert matcher.scored == []

    # And nothing was persisted with a score.
    row = db.execute(select(models.OpportunityMatch)).scalar_one()
    assert row.hard_gate_passed is False
    assert row.semantic_score is None
    assert row.state == models.OpportunityMatch.REJECTED_BY_RULE


def test_an_eligible_opportunity_does_get_scored(db, org):
    """The control case: the scorer must still run for a passing opportunity, or
    the test above would be satisfied by a scorer that never runs at all."""
    _ready_org(db, org)
    opportunity = _opportunity(db)
    matcher = Matcher(
        db, org, semantic_scorer=lambda candidate: 0.88, scorer_name="stub"
    )
    matches = matcher.evaluate([opportunity])
    db.commit()
    assert matches[0].qualification.passed
    assert matches[0].semantic_score == 0.88
    assert matches[0].rank == 1
    assert matcher.scored == [opportunity.id]


def test_an_ai_inferred_fact_cannot_satisfy_a_hard_gate(db, org):
    """The inference failure reaching the eligibility boundary.

    A guessed country must not make an organisation eligible in a country it is
    not registered in. Hard gates read only submission-safe, unexpired facts.
    """
    _fact(db, org, "country", "Uganda", state=models.OrgFact.AI_INFERRED, source="ai:model")
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_valid_until", "2030-01-01",
          valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc))
    opportunity = _opportunity(db, country="Uganda")

    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(opportunity)
    country = next(g for g in qualification.gates if g.gate == "country_eligible")
    assert country.outcome == UNKNOWN, "an AI-inferred country satisfied a hard gate"
    assert qualification.state == models.OpportunityMatch.NEEDS_DATA


# ---------------------------------------------------------------------------
# Individual gates
# ---------------------------------------------------------------------------
def test_an_inactive_listing_is_rejected(db, org):
    _ready_org(db, org)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, is_active=False)
    )
    assert qualification.state == models.OpportunityMatch.REJECTED_BY_RULE
    assert "opportunity_active" in qualification.failed_gates


def test_a_passed_deadline_is_rejected(db, org):
    _ready_org(db, org)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, deadline=past)
    )
    assert "deadline_open" in qualification.failed_gates
    assert qualification.state == models.OpportunityMatch.REJECTED_BY_RULE
    # The reason names the date, because "rejected" without a date is not
    # actionable for the person reading it.
    gate = next(g for g in qualification.gates if g.gate == "deadline_open")
    assert "passed on" in gate.reason
    assert past.date().isoformat() in gate.reason


def test_a_missing_deadline_is_unknown_not_a_pass(db, org):
    """A rolling or unstated deadline needs a human to read the guidelines."""
    _ready_org(db, org)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, deadline=None)
    )
    assert "deadline_open" in qualification.unknown_gates
    assert qualification.state == models.OpportunityMatch.NEEDS_DATA


def test_an_expired_registration_is_a_failure_not_an_unknown(db, org):
    """A lapsed certificate is a definite ineligibility.

    The difference matters: treating it as "needs data" would keep proposing
    ineligible applications until somebody noticed.
    """
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_valid_until", "2020-01-01",
          valid_until=datetime(2020, 1, 1, tzinfo=timezone.utc))
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "registration_valid" in qualification.failed_gates
    assert qualification.state == models.OpportunityMatch.REJECTED_BY_RULE


def test_a_missing_registration_is_unknown(db, org):
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "organisation_type", "NGO")
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "registration_valid" in qualification.unknown_gates
    assert qualification.state == models.OpportunityMatch.NEEDS_DATA


def test_a_registration_status_fact_is_accepted_as_a_fallback(db, org):
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_status", "active")
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "registration_valid" not in qualification.failed_gates
    assert "registration_valid" not in qualification.unknown_gates


def test_a_suspended_registration_fails(db, org):
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "organisation_type", "NGO")
    _fact(db, org, "registration_status", "suspended")
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "registration_valid" in qualification.failed_gates


def test_stated_eligibility_without_a_recorded_type_is_unknown(db, org):
    """The eligibility text is prose, so it cannot become a reliable rule.

    Rather than guess - and rather than silently pass - a listing that states
    criteria we have not modelled yields a review task.
    """
    _fact(db, org, "country", "Uganda")
    _fact(db, org, "registration_valid_until", "2030-01-01",
          valid_until=datetime(2030, 1, 1, tzinfo=timezone.utc))
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, eligibility_criteria="Registered NGOs in East Africa only")
    )
    assert "legal_status_eligible" in qualification.unknown_gates


def test_a_maximum_below_the_organisations_floor_is_rejected(db, org):
    """A preference, not a legal bar - but a definite one when it applies."""
    _ready_org(db, org)
    _fact(db, org, "minimum_grant_amount", 100_000)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, amount_max=5_000)
    )
    assert "funding_range_suitable" in qualification.failed_gates


def test_no_floor_means_the_gate_passes(db, org):
    """Treating an unset floor as UNKNOWN would stall matching for every
    organisation that never set one."""
    _ready_org(db, org)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "funding_range_suitable" not in qualification.unknown_gates
    assert "funding_range_suitable" not in qualification.failed_gates


def test_a_non_numeric_floor_is_unknown(db, org):
    _ready_org(db, org)
    _fact(db, org, "minimum_grant_amount", "lots")
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(_opportunity(db))
    assert "funding_range_suitable" in qualification.unknown_gates


def test_missing_required_documents_are_unknown(db, org):
    _ready_org(db, org)
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, eligibility_criteria="Applicants must attach a registration certificate")
    )
    assert "required_documents_available" in qualification.unknown_gates


def test_an_expired_approved_document_is_a_failure(db, org):
    """Definite, unlike a missing one."""
    _ready_org(db, org)
    vault = DocumentVault(db, org)
    document = vault.add_version(
        title="Registration", doc_type="registration_certificate",
        storage_key="org/reg.pdf", checksum_sha256=checksum_bytes(b"x"),
        mime_type="application/pdf",
        valid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )
    vault.approve(document, approved_by="user-1")
    db.commit()

    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, eligibility_criteria="Attach a registration certificate")
    )
    assert "required_documents_available" in qualification.failed_gates


def test_an_available_approved_document_passes_the_gate(db, org):
    _ready_org(db, org)
    vault = DocumentVault(db, org)
    document = vault.add_version(
        title="Registration", doc_type="registration_certificate",
        storage_key="org/reg.pdf", checksum_sha256=checksum_bytes(b"x"),
        mime_type="application/pdf",
        valid_until=datetime.now(timezone.utc) + timedelta(days=365),
    )
    vault.approve(document, approved_by="user-1")
    db.commit()

    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, eligibility_criteria="Attach a registration certificate")
    )
    assert "required_documents_available" not in qualification.failed_gates
    assert "required_documents_available" not in qualification.unknown_gates


def test_an_unapproved_document_does_not_satisfy_the_gate(db, org):
    """Uploading is not approving."""
    _ready_org(db, org)
    DocumentVault(db, org).add_version(
        title="Registration", doc_type="registration_certificate",
        storage_key="org/reg.pdf", checksum_sha256=checksum_bytes(b"x"),
        mime_type="application/pdf",
    )
    db.commit()
    qualification = EligibilityEngine(OrganisationMemory(db, org)).qualify(
        _opportunity(db, eligibility_criteria="Attach a registration certificate")
    )
    assert "required_documents_available" in qualification.unknown_gates


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def test_matches_are_ranked_by_score_then_deadline(db, org):
    """Between two equally good opportunities, the one closing sooner is the one
    that needs work started today."""
    _ready_org(db, org)
    soon = _opportunity(db, deadline=datetime.now(timezone.utc) + timedelta(days=5))
    later = _opportunity(db, deadline=datetime.now(timezone.utc) + timedelta(days=90))
    scores = {soon.id: 0.8, later.id: 0.8}

    matcher = Matcher(db, org, semantic_scorer=lambda c: scores[c.id], scorer_name="stub")
    matches = matcher.evaluate([later, soon])
    db.commit()

    ranked = sorted([m for m in matches if m.rank], key=lambda m: m.rank)
    assert [m.opportunity.id for m in ranked] == [soon.id, later.id]


def test_a_higher_score_outranks_a_sooner_deadline(db, org):
    _ready_org(db, org)
    soon = _opportunity(db, deadline=datetime.now(timezone.utc) + timedelta(days=5))
    later = _opportunity(db, deadline=datetime.now(timezone.utc) + timedelta(days=90))
    scores = {soon.id: 0.5, later.id: 0.95}

    matcher = Matcher(db, org, semantic_scorer=lambda c: scores[c.id], scorer_name="stub")
    matches = matcher.evaluate([soon, later])
    ranked = sorted([m for m in matches if m.rank], key=lambda m: m.rank)
    assert ranked[0].opportunity.id == later.id


def test_rejected_opportunities_are_not_ranked(db, org):
    _ready_org(db, org)
    good = _opportunity(db)
    bad = _opportunity(db, is_active=False)
    matcher = Matcher(db, org, semantic_scorer=lambda c: 0.9, scorer_name="stub")
    matches = matcher.evaluate([good, bad])
    by_id = {m.opportunity.id: m for m in matches}
    assert by_id[bad.id].rank is None
    assert by_id[bad.id].final_score is None
    assert by_id[good.id].rank == 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_rejections_are_stored_with_their_reasons(db, org):
    """An organisation that cannot see what it was ruled out of cannot correct
    its own profile."""
    _ready_org(db, org)
    matcher = Matcher(db, org)
    matcher.evaluate([_opportunity(db, is_active=False)])
    db.commit()

    row = db.execute(select(models.OpportunityMatch)).scalar_one()
    assert row.state == models.OpportunityMatch.REJECTED_BY_RULE
    assert "opportunity_active" in (row.failed_gates or {}).get("gates", [])
    assert row.reasons["summary"].startswith("rejected by rule")


def test_needs_data_rows_are_queryable_as_a_work_list(db, org):
    _fact(db, org, "organisation_type", "NGO")
    matcher = Matcher(db, org)
    matcher.evaluate([_opportunity(db)])
    db.commit()
    assert len(matcher.needs_data()) == 1
    assert matcher.matched() == []


def test_matched_returns_only_eligible_opportunities(db, org):
    _ready_org(db, org)
    matcher = Matcher(db, org, semantic_scorer=lambda c: 0.9, scorer_name="stub")
    matcher.evaluate([_opportunity(db), _opportunity(db, is_active=False)])
    db.commit()
    assert len(matcher.matched()) == 1


def test_re_evaluating_updates_rather_than_duplicating(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(db)
    Matcher(db, org).evaluate([opportunity])
    db.commit()
    Matcher(db, org).evaluate([opportunity])
    db.commit()
    assert len(db.execute(select(models.OpportunityMatch)).scalars().all()) == 1


def test_a_unique_constraint_stops_two_runs_claiming_one_verdict(db, org):
    _ready_org(db, org)
    opportunity = _opportunity(db)
    db.add(models.OpportunityMatch(
        org_id=org, opportunity_id=opportunity.id,
        state=models.OpportunityMatch.MATCHED, hard_gate_passed=True,
        computed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.add(models.OpportunityMatch(
        org_id=org, opportunity_id=opportunity.id,
        state=models.OpportunityMatch.MATCHED, hard_gate_passed=True,
        computed_at=datetime.now(timezone.utc),
    ))
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_matching_refuses_an_unknown_tenant(db):
    with pytest.raises(MatchingError) as excinfo:
        Matcher(db, "")
    assert "deny" in str(excinfo.value)


def test_one_tenants_matches_are_not_anothers(db, org):
    other_user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(other_user)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other NGO", slug="other-ngo", owner_user_id=other_user.id
    )
    db.add(other)
    db.commit()
    _ready_org(db, org)

    opportunity = _opportunity(db)
    Matcher(db, org, semantic_scorer=lambda c: 0.9).evaluate([opportunity])
    db.commit()

    assert len(Matcher(db, org).stored_results()) == 1
    assert Matcher(db, other.id).stored_results() == []
