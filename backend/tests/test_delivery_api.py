"""The delivery API surface.

Written because `_agent_for` was **referenced but never defined** in the first cut and
`py_compile` was perfectly happy: Python resolves global names at call time, not at
import. A route that raises `NameError` on its first request is exactly the bug a route
test catches and a compile does not.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from tests.test_agent_api import _clear, _client, _world  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "delivery_api.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _clear()


@pytest.fixture
def world(db):
    return _world(db)


@pytest.fixture
def grant(db, world):
    """A grant with a condition, a report and a tranche."""
    from agent.delivery.service import DeliveryService

    agent = world["service"].get()
    package = models.SubmissionPackage(
        id=str(uuid.uuid4()),
        org_id=world["org"].id,
        agent_id=agent.id,
        application_id=world["application"].id,
        opportunity_id=world["application"].opportunity_id,
        package_fingerprint="b" * 64,
        manifest={
            "documents": [], "answers": [],
            "budget": {"currency": "UGX", "total": 120_000_000,
                       "lines": [{"item": "staff", "amount": 120_000_000}]},
            "contact_email": "grants@warchild.org", "target_url": "https://funder.example.org",
        },
        application_version=1,
        status=models.SubmissionPackage.SUBMITTED,
        submission_mode=models.SubmissionPackage.MODE_HANDOFF,
        idempotency_key=f"app:{uuid.uuid4().hex}",
        created_at=_now(), authorised_at=_now(), submitted_at=_now(),
    )
    db.add(package)
    db.commit()

    service = DeliveryService(db, org_id=world["org"].id, agent_id=agent.id)
    result = service.handover(
        package=package, reference="GRANT-API-1", awarded_amount=120_000_000,
        conditions=[{"title": "Signed agreement",
                     "kind": models.GrantCondition.KIND_PRECONDITION,
                     "due_on": _now() + timedelta(days=5)}],
        reporting_schedule=[{"title": "Q1 narrative", "due_on": _now() + timedelta(days=20)}],
        disbursement_schedule=[{"amount": 60_000_000, "label": "First tranche",
                                "expected_on": _now() + timedelta(days=10)}],
    )
    db.commit()
    return result


# ===========================================================================
# THE ROUTES ACTUALLY RUN
# ===========================================================================
def test_the_grants_route_answers(db, world, grant):
    client = _client(db, world)
    response = client.get("/api/v1/agent/grants")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] == 1
    assert body["grants"][0]["reference"] == "GRANT-API-1"
    assert body["grants"][0]["currency"] == "UGX"
    # Money as a number, so a client does not have to parse it.
    assert body["grants"][0]["awarded_amount"] == 120_000_000.0
    # Provenance is exposed, so "which authorised package is this from" is answerable.
    assert body["grants"][0]["source_package_id"]


def test_the_grant_detail_route_returns_its_obligations(db, world, grant):
    client = _client(db, world)
    response = client.get(f"/api/v1/agent/grants/{grant.grant_id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["grant"]["reference"] == "GRANT-API-1"
    assert len(body["conditions"]) == 1
    assert len(body["reporting_obligations"]) == 1
    assert len(body["disbursements"]) == 1
    # The workplan is derived, and the route shows its provenance.
    assert body["project"]["baseline_workplan"]["source"] == "submission_package.approved_budget"


def test_a_condition_with_no_evidence_reports_no_evidence(db, world, grant):
    """A client can treat a null evidence_ref as 'not actually evidenced', because the
    service refuses to set SATISFIED without one."""
    client = _client(db, world)
    body = client.get(f"/api/v1/agent/grants/{grant.grant_id}").json()
    condition = body["conditions"][0]
    assert condition["status"] == models.GrantCondition.STATUS_OPEN
    assert condition["evidence_ref"] is None


def test_the_grant_detail_route_404s_an_unknown_id(db, world, grant):
    client = _client(db, world)
    response = client.get(f"/api/v1/agent/grants/{uuid.uuid4()}")
    assert response.status_code == 404


def test_the_deadlines_route_gathers_all_three_sources(db, world, grant):
    client = _client(db, world)
    response = client.get("/api/v1/agent/deadlines?within_days=30")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["count"] == 3
    kinds = {d["kind"] for d in body["deadlines"]}
    assert kinds == {"CONDITION", "REPORT", "DISBURSEMENT"}
    # Soonest first.
    days = [d["days_remaining"] for d in body["deadlines"]]
    assert days == sorted(days)


def test_the_deadlines_route_respects_the_horizon(db, world, grant):
    client = _client(db, world)
    body = client.get("/api/v1/agent/deadlines?within_days=1").json()
    assert body["count"] == 0


def test_the_compliance_route_answers_with_three_separate_lists(db, world, grant):
    client = _client(db, world)
    response = client.get("/api/v1/agent/compliance")
    assert response.status_code == 200, response.text

    body = response.json()
    # Not a score: the three risks stay separate because the response to each differs.
    assert set(body) >= {
        "blocking_conditions", "overdue_reports", "late_disbursements", "portfolio", "counts"
    }
    assert body["counts"]["blocking_conditions"] == 1
    assert body["grants_active"] == 1


def test_the_compliance_route_reports_money_in_the_bank(db, world, grant):
    client = _client(db, world)
    body = client.get("/api/v1/agent/compliance").json()
    portfolio = body["portfolio"]
    assert portfolio["scheduled_total"] == "60000000.00"
    assert portfolio["received_total"] == "0"
    assert portfolio["outstanding_total"] == "60000000.00"


def test_the_routes_are_empty_rather_than_broken_for_a_new_organisation(db, world, grant):
    """An organisation with no grants renders an empty state, not a 500."""
    other_org, other_service = _other_org(db)
    client = _client(db, world, org_id=other_org.id)

    grants = client.get("/api/v1/agent/grants")
    assert grants.status_code == 200
    assert grants.json()["count"] == 0

    deadlines = client.get("/api/v1/agent/deadlines")
    assert deadlines.status_code == 200
    assert deadlines.json()["count"] == 0


def test_one_organisations_grant_is_not_visible_to_another(db, world, grant):
    """Tenancy, through the API rather than through the service."""
    other_org, _other_service = _other_org(db)
    client = _client(db, world, org_id=other_org.id)

    response = client.get(f"/api/v1/agent/grants/{grant.grant_id}")
    assert response.status_code == 404, "another organisation's grant was readable"


def _other_org(db):
    from agent.granada_agent import GranadaAgentService

    user = models.User(id=str(uuid.uuid4()), display_name="Other Owner")
    db.add(user)
    db.commit()
    org = models.Organisation(
        id=str(uuid.uuid4()), name="Other NGO",
        slug=f"other-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    service = GranadaAgentService(db, org.id)
    service.provision(autonomy="MONITOR_ONLY")
    db.commit()
    return org, service
