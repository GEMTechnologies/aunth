"""The funding screen's API: what discovery found, and what the matcher decided.

Before this route existed the pipeline was fully operational and entirely invisible: 112 opportunities
and 95 verdicts in the database, reachable only through psql. That is why "are the funding bots
working?" could not be answered from the product.

THE ASSERTIONS THAT MATTER, and each maps to a way the screen could lie:

  * an opportunity the matcher has not reached yet must APPEAR, not vanish - the left join;
  * a rejection must carry the gate that caused it, because an organisation that cannot see what it
    was ruled out of cannot correct its profile;
  * a blocked match must say which FACT would unblock it, because "95 NEEDS_DATA" is not actionable
    and "95 waiting on your country" is;
  * one organisation's verdicts must never appear in another's list.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from database import get_db  # noqa: E402
from router import get_current_user, get_tenant_context  # noqa: E402
from tenant_context import TenantContext  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "oppapi.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="Granada Test Organisation",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    return {"org": org, "user": user}


def opportunity(db, *, title="Community Development Grant", country="Uganda", deadline_days=30):
    row = models.Opportunity(
        id=str(uuid.uuid4()), title=title,
        source_url=f"https://grants.example/{uuid.uuid4().hex[:8]}",
        source_name="Grants.gov", country=country,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        amount_max=500000, currency="USD", is_active=True,
        deadline=datetime.now(timezone.utc) + timedelta(days=deadline_days),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def match(db, world, opp, *, state, passed=False, gates=None, reasons=None, score=None, rank=None):
    row = models.OpportunityMatch(
        id=str(uuid.uuid4()), org_id=world["org"].id, opportunity_id=opp.id,
        state=state, hard_gate_passed=passed,
        unknown_gates={"gates": gates} if gates else {"gates": []},
        failed_gates={"gates": gates} if gates else {"gates": []},
        reasons=reasons,
        final_score=score, rank=rank,
        contract_version="v1", computed_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def client(db, world):
    import main

    org_id = str(world["org"].id)
    user = world["user"]

    def _db_override():
        yield db

    def _tenant_override():
        return TenantContext(user_id=user.id, org_ids=(org_id,), primary_org_id=org_id)

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def get(client, **params):
    return client.get("/api/v1/agent/opportunities", params=params).json()


# ---------------------------------------------------------------------------
# An unmatched opportunity still appears
# ---------------------------------------------------------------------------
def test_an_empty_catalogue_is_empty_not_an_error(client):
    body = get(client)
    assert body["opportunities"] == []
    assert body["catalogue_size"] == 0
    assert body["summary"]["matched"] == 0


def test_an_opportunity_the_matcher_has_not_reached_appears_with_no_match(client, db, world):
    """A LEFT join. The bot found it and the matcher has not judged it - that is a real state, and an
    inner join would hide it and make the catalogue look smaller than it is."""
    opportunity(db, title="Fresh from the bot")
    body = get(client)
    assert len(body["opportunities"]) == 1
    assert body["opportunities"][0]["match"] is None
    assert body["summary"]["not_evaluated"] == 1


# ---------------------------------------------------------------------------
# The verdicts, with their evidence
# ---------------------------------------------------------------------------
def test_a_matched_opportunity_carries_its_score_and_rank(client, db, world):
    opp = opportunity(db, title="Best fit")
    match(db, world, opp, state=models.OpportunityMatch.MATCHED, passed=True, score=0.91, rank=1)

    row = get(client)["opportunities"][0]
    assert row["match"]["state"] == "MATCHED"
    assert row["match"]["hard_gate_passed"] is True
    assert row["match"]["final_score"] == pytest.approx(0.91)
    assert row["match"]["rank"] == 1


def test_a_blocked_match_names_the_fact_that_would_unblock_it(client, db, world):
    """THE POINT OF THE WHOLE ROUTE. "95 NEEDS_DATA" is not actionable; "waiting on your country" is,
    and it points at the profile screen that can fix it."""
    opp = opportunity(db)
    match(
        db, world, opp, state=models.OpportunityMatch.NEEDS_DATA,
        gates=["country_eligible", "registration_valid"],
        reasons={"state": "NEEDS_DATA", "gates": [
            {"gate": "opportunity_active", "outcome": "PASS", "reason": "the listing is active"},
            {"gate": "country_eligible", "outcome": "UNKNOWN", "reason": "country is not known"},
        ]},
    )

    row = get(client)["opportunities"][0]
    needs = {b["gate"]: b["needs_facts"] for b in row["match"]["blocking"]}
    assert needs["country_eligible"] == ["country"]
    assert "registration_status" in needs["registration_valid"]
    # The gate evidence travels with the verdict.
    assert any(g["gate"] == "country_eligible" and g["outcome"] == "UNKNOWN"
               for g in row["match"]["gate_reasons"])


def test_a_rejection_is_shown_with_the_gate_that_caused_it(client, db, world):
    """`REJECTED_BY_RULE` rows are kept on purpose: an organisation that cannot see what it was ruled
    out of cannot correct its profile."""
    opp = opportunity(db)
    match(db, world, opp, state=models.OpportunityMatch.REJECTED_BY_RULE, gates=["country_eligible"])

    row = get(client)["opportunities"][0]
    assert row["match"]["state"] == "REJECTED_BY_RULE"
    assert row["match"]["failed_gates"] == ["country_eligible"]


def test_the_summary_counts_the_whole_catalogue_not_the_page(client, db, world):
    for index in range(3):
        opp = opportunity(db, title=f"Opp {index}")
        match(db, world, opp, state=models.OpportunityMatch.NEEDS_DATA, gates=["country_eligible"])
    get(client, limit=1)
    body = get(client, limit=1)
    assert len(body["opportunities"]) == 1
    assert body["summary"]["needs_data"] == 3, "a page-sized summary would be mistaken for the portfolio"


def test_blocked_by_fact_aggregates_across_matches(client, db, world):
    for index in range(4):
        opp = opportunity(db, title=f"Opp {index}")
        match(db, world, opp, state=models.OpportunityMatch.NEEDS_DATA,
              gates=["country_eligible", "registration_valid"])
    body = get(client)
    assert body["blocked_by_fact"]["country"] == 4
    assert body["blocked_by_fact"]["registration_status"] == 4


# ---------------------------------------------------------------------------
# Ordering: attention at the top
# ---------------------------------------------------------------------------
def test_matched_comes_before_blocked_before_rejected_before_unjudged(client, db, world):
    rejected = opportunity(db, title="Rejected")
    match(db, world, rejected, state=models.OpportunityMatch.REJECTED_BY_RULE, gates=["country_eligible"])
    unjudged = opportunity(db, title="Unjudged")
    blocked = opportunity(db, title="Blocked")
    match(db, world, blocked, state=models.OpportunityMatch.NEEDS_DATA, gates=["country_eligible"])
    matched = opportunity(db, title="Matched")
    match(db, world, matched, state=models.OpportunityMatch.MATCHED, passed=True, score=0.8, rank=1)

    titles = [row["title"] for row in get(client)["opportunities"]]
    assert titles == ["Matched", "Blocked", "Rejected", "Unjudged"]


# ---------------------------------------------------------------------------
# Filtering and paging
# ---------------------------------------------------------------------------
def test_filter_by_state(client, db, world):
    matched = opportunity(db, title="Matched one")
    match(db, world, matched, state=models.OpportunityMatch.MATCHED, passed=True, score=0.7)
    blocked = opportunity(db, title="Blocked one")
    match(db, world, blocked, state=models.OpportunityMatch.NEEDS_DATA, gates=["country_eligible"])

    rows = get(client, state="MATCHED")["opportunities"]
    assert [r["title"] for r in rows] == ["Matched one"]


def test_an_unknown_state_is_a_400_not_an_empty_list(client):
    resp = client.get("/api/v1/agent/opportunities", params={"state": "PROBABLY"})
    assert resp.status_code == 400


def test_pagination_does_not_repeat(client, db, world):
    for index in range(5):
        opportunity(db, title=f"Opp {index}")
    first = get(client, limit=2)
    second = get(client, limit=2, offset=2)
    assert first["next_offset"] == 2
    assert {r["id"] for r in first["opportunities"]}.isdisjoint(
        {r["id"] for r in second["opportunities"]}
    )


def test_the_limit_is_bounded(client, db, world):
    opportunity(db)
    assert get(client, limit=100000)["limit"] == 200


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
def test_another_organisations_verdicts_are_invisible(db, world):
    """The catalogue is shared; the verdict is not. Who is pursuing which funding is among the most
    commercially sensitive things in the product."""
    import main

    stranger = models.User(id=str(uuid.uuid4()), display_name="Stranger")
    db.add(stranger)
    db.commit()
    other = models.Organisation(
        id=str(uuid.uuid4()), name="Other", slug=f"org-{uuid.uuid4().hex[:8]}",
        owner_user_id=stranger.id,
    )
    db.add(other)
    db.commit()

    opp = opportunity(db, title="Shared opportunity")
    # The victim organisation matched it; the stranger has no verdict.
    match(db, world, opp, state=models.OpportunityMatch.MATCHED, passed=True, score=0.95, rank=1)

    def _db_override():
        yield db

    def _tenant_override():
        return TenantContext(user_id=stranger.id, org_ids=(str(other.id),),
                             primary_org_id=str(other.id))

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: stranger
    try:
        client = TestClient(main.app)
        body = client.get("/api/v1/agent/opportunities").json()
        # The SHARED opportunity is visible - it belongs to nobody.
        assert len(body["opportunities"]) == 1
        # But the other tenant's verdict is not.
        assert body["opportunities"][0]["match"] is None
        assert body["summary"]["matched"] == 0
    finally:
        main.app.dependency_overrides.clear()
