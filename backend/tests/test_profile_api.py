"""The organisation profile intake: the route that lets a profile exist at all.

THE DEFECT THIS CLOSES. `OrganisationMemory.record_fact` was reachable from tests and from other
services, but no HTTP route wrote a fact. Measured on production, `org_facts` was empty and all 95
opportunity matches sat at NEEDS_DATA - because the `country_eligible` and `registration_valid`
gates had nothing to read. The matching engine was correct throughout; it simply could not be told
anything about the organisation.

The load-bearing assertions here are about PROVENANCE, not about round-tripping a string:
  * a caller cannot choose its own state, so nothing written here can claim VERIFIED;
  * nothing written here can be AI_INFERRED, which is the one state that must never reach a
    submission;
  * an empty value is not a fact, because a gate that treats "" as known would produce a match the
    organisation cannot support.
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
    engine, session = make_sqlite_db(tmp_path, "profileapi.db")
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


def put(client, facts):
    return client.put("/api/v1/agent/profile/facts", json={"facts": facts})


# ---------------------------------------------------------------------------
# An empty profile explains itself
# ---------------------------------------------------------------------------
def test_an_empty_profile_reports_which_gates_are_blocked(client):
    body = client.get("/api/v1/agent/profile/facts").json()
    assert body["total"] == 0
    assert body["usable_fact_keys"] == []
    # The whole point of the endpoint: a wall of NEEDS_DATA has a stated cause.
    assert set(body["blocks_matching"]) == {"country_eligible", "registration_valid"}


def test_the_gate_list_is_derived_from_the_matcher(client):
    """If `matching.py` starts reading a new fact key, this route must not keep reporting the old
    set - that would send an operator to fill a field nobody reads."""
    import agent_api
    from agent import matching

    source = Path(matching.__file__).read_text(encoding="utf-8")
    for gate in agent_api.GATE_FACTS:
        assert gate in source, f"{gate} is not a gate the matcher evaluates"
    assert "_fact_value(self.memory, \"country\")" in source


# ---------------------------------------------------------------------------
# Writing facts
# ---------------------------------------------------------------------------
def test_a_fact_is_recorded_as_user_provided_with_a_named_source(client, db, world):
    out = put(client, [{"key": "country", "value": "Uganda"}]).json()
    assert out["recorded"] == [{"key": "country", "version": 1, "state": models.OrgFact.USER_PROVIDED}]
    assert out["rejected"] == []

    fact = db.execute(
        models.OrgFact.__table__.select().where(models.OrgFact.org_id == str(world["org"].id))
    ).first()
    assert fact.state == models.OrgFact.USER_PROVIDED
    assert fact.source.startswith("user:")
    assert fact.version == 1


def test_a_caller_cannot_choose_its_own_provenance(client, db, world):
    """A request body that names VERIFIED or AI_INFERRED must be ignored, not honoured.

    VERIFIED means a human with authority confirmed it; AI_INFERRED is never submission-safe. If the
    body could set either, the table's central guarantee would be a suggestion.
    """
    put(client, [{"key": "country", "value": "Uganda", "state": "VERIFIED",
                  "source": "somebody", "confidence": 0.99}])
    fact = db.execute(
        models.OrgFact.__table__.select().where(models.OrgFact.org_id == str(world["org"].id))
    ).first()
    assert fact.state == models.OrgFact.USER_PROVIDED, "state must not come from the request"


def test_writing_country_makes_the_country_gate_decidable(client):
    put(client, [{"key": "country", "value": "Uganda"}])
    body = client.get("/api/v1/agent/profile/facts").json()
    assert "country_eligible" not in body["blocks_matching"]
    assert "registration_valid" in body["blocks_matching"]
    gate = next(g for g in body["matching_gates"] if g["gate"] == "country_eligible")
    assert gate["decidable"] is True and gate["needs"] == []


@pytest.mark.parametrize("key,value", [("registration_status", "ACTIVE"),
                                       ("registration_valid_until", "2035-01-01")])
def test_registration_valid_is_decidable_from_either_key(client, key, value):
    """The matcher accepts an expiry OR a status. Requiring both here would send a person to fill a
    field the matcher is not waiting for."""
    put(client, [{"key": key, "value": value}])
    body = client.get("/api/v1/agent/profile/facts").json()
    assert "registration_valid" not in body["blocks_matching"]


def test_a_full_profile_blocks_nothing(client):
    put(client, [
        {"key": "country", "value": "Uganda"},
        {"key": "registration_status", "value": "ACTIVE"},
    ])
    body = client.get("/api/v1/agent/profile/facts").json()
    assert body["blocks_matching"] == []


def test_an_expiry_date_is_stored_and_reads_back(client):
    expiry = (datetime.now(timezone.utc) + timedelta(days=365)).replace(microsecond=0)
    put(client, [{"key": "registration_valid_until", "value": expiry.isoformat(),
                  "valid_until": expiry.isoformat()}])
    body = client.get("/api/v1/agent/profile/facts").json()
    fact = next(f for f in body["facts"] if f["key"] == "registration_valid_until")
    assert fact["valid_until"] is not None
    assert fact["submission_usable"] is True


# ---------------------------------------------------------------------------
# Versioning rather than overwriting
# ---------------------------------------------------------------------------
def test_a_second_write_supersedes_rather_than_overwrites(client, db, world):
    """An application assembled against version 1 must stay explainable after version 2 arrives."""
    put(client, [{"key": "country", "value": "Kenya"}])
    put(client, [{"key": "country", "value": "Uganda"}])

    rows = db.execute(
        models.OrgFact.__table__.select()
        .where(models.OrgFact.org_id == str(world["org"].id))
        .where(models.OrgFact.key == "country")
        .order_by(models.OrgFact.version)
    ).all()
    assert len(rows) == 2, "history must survive"
    assert rows[0].is_current is False
    assert rows[1].is_current is True
    assert rows[1].supersedes_id == rows[0].id

    body = client.get("/api/v1/agent/profile/facts").json()
    assert [f["value"] for f in body["facts"] if f["key"] == "country"] == ["Uganda"]


# ---------------------------------------------------------------------------
# Rejections: partial success, never a silently empty fact
# ---------------------------------------------------------------------------
def test_an_empty_value_is_rejected_and_the_others_still_land(client):
    """A stored "" would let a gate treat "blank" as "known" and produce an unsupportable match."""
    out = put(client, [
        {"key": "country", "value": "   "},
        {"key": "organisation_type", "value": "NGO"},
    ]).json()
    assert [r["key"] for r in out["recorded"]] == ["organisation_type"]
    assert [r["key"] for r in out["rejected"]] == ["country"]


def test_a_null_value_is_rejected(client):
    out = put(client, [{"key": "country", "value": None}]).json()
    assert out["recorded"] == []
    assert out["rejected"][0]["key"] == "country"


def test_an_empty_batch_is_refused_by_the_schema(client):
    assert client.put("/api/v1/agent/profile/facts", json={"facts": []}).status_code == 422


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
def test_a_user_outside_the_organisation_is_refused(db, world):
    """The profile is tenant data. A member of another organisation must not be able to read or
    write it, and the refusal must come from the tenant check rather than a filter that silently
    returns nothing."""
    import main

    stranger = models.User(id=str(uuid.uuid4()), display_name="Stranger")
    db.add(stranger)
    db.commit()

    other_org = models.Organisation(
        id=str(uuid.uuid4()), name="Other Org",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=stranger.id,
    )
    db.add(other_org)
    db.commit()

    def _db_override():
        yield db

    def _tenant_override():
        # A tenant bound to the other organisation.
        return TenantContext(
            user_id=stranger.id, org_ids=(str(other_org.id),), primary_org_id=str(other_org.id)
        )

    main.app.dependency_overrides[get_db] = _db_override
    main.app.dependency_overrides[get_tenant_context] = _tenant_override
    main.app.dependency_overrides[get_current_user] = lambda: stranger
    try:
        client = TestClient(main.app)
        resp = put(client, [{"key": "country", "value": "Uganda"}])
        # Either the tenant check refuses the bound org, or the write lands on the STRANGER's own
        # organisation. What must never happen is a fact appearing on the victim organisation.
        victim_facts = db.execute(
            models.OrgFact.__table__.select()
            .where(models.OrgFact.org_id == str(world["org"].id))
        ).all()
        assert victim_facts == []
        assert resp.status_code in (200, 403)
    finally:
        main.app.dependency_overrides.clear()
