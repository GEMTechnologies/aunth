"""Approving a document: the last human gate before the portal.

`DocumentVault.approve` existed and no route called it, so a generated cover letter was invisible and
unapprovable. Assembly then refused to freeze the package - correctly - and the chain stopped one step
from the portal with nothing in the product to unblock it.

These tests pin the two properties that matter: approval is attributed to a person, and the guards that
stop the wrong bytes being approved still fire through the route rather than being bypassed by it.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.decision.policy import Autonomy  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.organisation_memory import DocumentVault, checksum_bytes  # noqa: E402
from database import get_db  # noqa: E402
from router import get_current_user, get_tenant_context  # noqa: E402
from tenant_context import TenantContext  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "docapprove.db")
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
        id=str(uuid.uuid4()), name="Approval Test Org",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(org)
    db.commit()
    service = GranadaAgentService(db, org.id)
    service.provision(autonomy=Autonomy.MONITOR_ONLY)
    db.commit()

    vault = DocumentVault(db, org.id)
    document = vault.add_version(
        title="Cover Letter",
        doc_type="cover_letter",
        storage_key=f"generated/{org.id}/opp/cover.md",
        checksum_sha256=checksum_bytes(b"generated cover letter"),
        mime_type="text/markdown",
        size_bytes=26,
        uploaded_by=f"agent:{service.get().id}",
    )
    db.commit()
    return {"org": org, "user": user, "agent": service.get(), "document": document}


@pytest.fixture
def client(db, world):
    import main

    org_id = str(world["org"].id)
    user = world["user"]

    def _db():
        yield db

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        user_id=user.id, org_ids=(org_id,), primary_org_id=org_id
    )
    main.app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_generated_documents_are_listed_with_their_approval_state(client):
    resp = client.get("/api/v1/agent/documents")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["documents"][0]["doc_type"] == "cover_letter"
    assert body["documents"][0]["approval_status"] == models.Document.PENDING
    assert body["pending_approval"] == 1, "the route must say something is waiting"


def test_approving_attributes_it_to_the_person_who_did_it(client, world):
    document_id = str(world["document"].id)
    resp = client.post(f"/api/v1/agent/documents/{document_id}/approve")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["approval_status"] == models.Document.APPROVED
    assert out["approved_by"] == str(world["user"].id)
    assert out["approved_at"]


def test_a_superseded_version_cannot_be_approved(client, db, world):
    """Approving an old version would approve bytes that are no longer the current file - the silent
    mismatch between what was approved and what would be attached."""
    document = world["document"]
    document.is_current = False
    db.commit()

    resp = client.post(f"/api/v1/agent/documents/{document.id}/approve")
    assert resp.status_code == 409
    assert "current" in resp.json()["detail"]


def test_a_document_with_no_checksum_cannot_be_approved(client, db, world):
    """The guard rejects an empty checksum as well as a missing one - the column is NOT NULL, so an
    empty string is the shape a half-written row would actually take."""
    document = world["document"]
    document.checksum_sha256 = ""
    db.commit()

    resp = client.post(f"/api/v1/agent/documents/{document.id}/approve")
    assert resp.status_code == 409
    assert "checksum" in resp.json()["detail"]


def test_an_unknown_document_is_not_found(client):
    resp = client.post(f"/api/v1/agent/documents/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


def test_rejection_keeps_the_document_and_records_who(client, world):
    document_id = str(world["document"].id)
    resp = client.post(f"/api/v1/agent/documents/{document_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["approval_status"] == models.Document.REJECTED
    assert world["document"].approved_by == str(world["user"].id)


def test_a_rejection_records_why_as_well_as_who(client, world):
    """A rejection that names only the person leaves the next one guessing what to change, and an
    audit trail that answers "who" but not "why" cannot explain itself to the organisation."""
    document_id = str(world["document"].id)
    resp = client.post(
        f"/api/v1/agent/documents/{document_id}/reject",
        params={"reason": "the registration number is wrong"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejection_reason"] == "the registration number is wrong"
    assert world["document"].rejection_reason == "the registration number is wrong"

    # And it is visible where the document is listed, not only in the response.
    listing = client.get("/api/v1/agent/documents").json()
    assert listing["documents"][0]["rejection_reason"] == "the registration number is wrong"


def test_a_rejection_without_a_reason_is_still_a_rejection(client, world):
    document_id = str(world["document"].id)
    resp = client.post(f"/api/v1/agent/documents/{document_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["approval_status"] == models.Document.REJECTED
    assert resp.json()["rejection_reason"] is None
