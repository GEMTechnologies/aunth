"""The application package assembler.

`SubmissionPackage` has existed since Phase 8 with `package_fingerprint`, `manifest`,
`idempotency_key` and `handoff_ready_at`, and NOTHING created one - `SubmissionPackage(` appeared in
`models.py` and nowhere else. The pipeline reached prepared documents and stopped, with the table
describing what a funder receives sitting empty.

These tests exercise the assembler's actual behaviour: fingerprint identity, idempotent reuse,
the NEEDS_DATA distinction, and tenant isolation.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent import packaging  # noqa: E402
from agent.packaging import (  # noqa: E402
    EVIDENCE_TYPES,
    PackageItem,
    assemble,
    fingerprint_of,
    operator_message,
)


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "packaging.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="Packaging NGO"):
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name, slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row


def _agent(db, org):
    row = models.GranadaAgent(
        id=str(uuid.uuid4()), org_id=org.id, display_name="Agent", status=models.GranadaAgent.ACTIVE
    )
    db.add(row)
    db.commit()
    return row


def _opportunity(db):
    row = models.Opportunity(
        title="Community Health Grant 2027",
        source_url=f"https://funder.example.org/{uuid.uuid4().hex[:8]}",
        source_name="Example Funder",
        country="Nigeria",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        is_active=True,
        deadline=datetime.now(timezone.utc) + timedelta(days=30),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def _application(db, org, opportunity, version=1):
    row = models.Application(
        id=str(uuid.uuid4()), org_id=org.id, opportunity_id=opportunity.id,
        state="PREPARING", version=version, created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def _document(db, org, *, doc_type="cover_letter", checksum=None, version=1):
    row = models.Document(
        id=str(uuid.uuid4()), org_id=org.id, title=f"{doc_type} v{version}", doc_type=doc_type,
        storage_key=f"generated/{org.id}/{doc_type}-v{version}.md",
        checksum_sha256=checksum or uuid.uuid4().hex + uuid.uuid4().hex,
        mime_type="text/markdown", version=version, is_current=True,
        approval_status=models.Document.APPROVED,
    )
    db.add(row)
    db.commit()
    return row


def _assemble(db, org, agent, opportunity, application, documents, required):
    return assemble(
        db, org_id=org.id, agent_id=str(agent.id), application=application,
        opportunity=opportunity, organisation=org, documents=documents, required_types=required,
    )


# ===========================================================================
# THE FINGERPRINT
# ===========================================================================
def test_the_same_inputs_produce_the_same_fingerprint():
    """The fingerprint IS the identity. Without this nothing below holds."""
    items = [PackageItem("d1", "cover_letter", "Cover", 1, "a" * 64, "k", "text/markdown")]
    a, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=items)
    b, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=items)
    assert a == b


def test_a_CHANGED_document_produces_a_different_fingerprint():
    """THE property that makes authorisation meaningful.

    A human authorises a specific set of documents at specific versions. Change one - a re-issued
    audited statement, a corrected budget - and the authorisation must not silently still apply.
    """
    before = [PackageItem("d1", "cover_letter", "Cover", 1, "a" * 64, "k", "text/markdown")]
    after = [PackageItem("d1", "cover_letter", "Cover", 2, "b" * 64, "k", "text/markdown")]
    a, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=before)
    b, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=after)
    assert a != b, "a changed document did not change the fingerprint"


def test_the_fingerprint_does_NOT_depend_on_document_ORDER():
    """An unordered fingerprint would produce a new revision whenever the database chose a
    different plan - a bug that only appears under load."""
    one = PackageItem("d1", "cover_letter", "C", 1, "a" * 64, "k", "text/markdown")
    two = PackageItem("d2", "budget", "B", 1, "b" * 64, "k", "text/markdown")
    forward, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=[one, two])
    reverse, _ = fingerprint_of(org_id="o", opportunity_id="p", application_version=1, items=[two, one])
    assert forward == reverse


def test_a_different_organisation_produces_a_different_fingerprint():
    """Tenant identity is part of the package identity, so one NGO's fingerprint can never collide
    with another's and be reused across the boundary."""
    items = [PackageItem("d1", "cover_letter", "C", 1, "a" * 64, "k", "text/markdown")]
    a, _ = fingerprint_of(org_id="org-a", opportunity_id="p", application_version=1, items=items)
    b, _ = fingerprint_of(org_id="org-b", opportunity_id="p", application_version=1, items=items)
    assert a != b


# ===========================================================================
# ASSEMBLY
# ===========================================================================
def test_a_complete_set_assembles_a_DRAFT_package(db):
    org, agent = _org(db), None
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    docs = [_document(db, org, doc_type="cover_letter"), _document(db, org, doc_type="budget")]

    result = _assemble(db, org, agent, opp, application, docs, ["cover_letter", "budget"])
    db.commit()

    assert result.status == models.SubmissionPackage.DRAFT
    assert not result.missing
    assert len(result.included) == 2
    assert db.query(models.SubmissionPackage).count() == 1


def test_the_package_is_MODE_HANDOFF(db):
    """It prepares everything and performs NO external action. Nothing may claim otherwise."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    _assemble(db, org, agent, opp, application, [_document(db, org)], ["cover_letter"])
    db.commit()

    package = db.query(models.SubmissionPackage).first()
    assert package.submission_mode == models.SubmissionPackage.MODE_HANDOFF
    assert package.status != models.SubmissionPackage.SUBMITTED
    assert package.submitted_at is None


def test_a_MISSING_EVIDENCE_document_is_NEEDS_DATA_not_a_failure(db):
    """A registration certificate the NGO has not uploaded is the ORGANISATION's outstanding action.
    Reporting it as an infrastructure failure is how a healthy system looks broken."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)

    result = _assemble(
        db, org, agent, opp, application,
        [_document(db, org, doc_type="cover_letter")],
        ["cover_letter", "registration_certificate"],
    )
    db.commit()

    assert result.status == models.SubmissionPackage.NEEDS_DATA
    assert "registration_certificate" in result.needs_data
    assert result.needs_data, "an evidence gap was not classified as the organisation's to close"


def test_the_operator_message_names_the_blocker_and_the_count(db):
    """"Package failed" tells nobody what to do next."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)

    result = _assemble(
        db, org, agent, opp, application, [_document(db, org)],
        ["cover_letter", "audited_accounts"],
    )
    message = operator_message(result)
    assert "audited_accounts" in message
    assert "blocked" in message.lower()


def test_the_operator_message_NEVER_claims_submission(db):
    """A successful assembly is not a successful application.

    The model's own docstring is explicit that a SUBMITTED state requires a receipt. A message that
    said "completed" after assembly would tell an NGO it had applied when it had not.
    """
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    result = _assemble(db, org, agent, opp, application, [_document(db, org)], ["cover_letter"])
    message = operator_message(result).lower()
    assert "submitted" not in message
    assert "no submission has been made" in message


def test_the_manifest_names_the_organisation_and_opportunity(db):
    org = _org(db, name="Distinct NGO Name")
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    result = _assemble(db, org, agent, opp, application, [_document(db, org)], ["cover_letter"])

    assert result.manifest["organisation"]["name"] == "Distinct NGO Name"
    assert result.manifest["opportunity"]["id"] == str(opp.id)
    assert result.manifest["submission_mode"] == models.SubmissionPackage.MODE_HANDOFF


def test_the_manifest_carries_CHECKSUMS_not_contents(db):
    """A manifest is copied into audit records and shown to an operator. It must identify a document,
    not carry it."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    doc = _document(db, org)
    result = _assemble(db, org, agent, opp, application, [doc], ["cover_letter"])

    entry = result.manifest["documents"][0]
    assert entry["checksum_sha256"] == doc.checksum_sha256
    assert "storage_key" not in entry, "the manifest leaked a storage path"


# ===========================================================================
# IDEMPOTENCY
# ===========================================================================
def test_assembling_twice_REUSES_the_package(db):
    """A retry, or a second worker racing the first, must not inflate the revision count."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)
    docs = [_document(db, org)]

    first = _assemble(db, org, agent, opp, application, docs, ["cover_letter"])
    db.commit()
    second = _assemble(db, org, agent, opp, application, docs, ["cover_letter"])
    db.commit()

    assert first.reused is False
    assert second.reused is True, "a re-assembly created a new package instead of reusing it"
    assert db.query(models.SubmissionPackage).count() == 1


def test_a_CHANGED_document_creates_a_NEW_package(db):
    """The inverse. A new document version is a genuinely different set, and the authorisation the
    human gave applied to the previous one."""
    org = _org(db)
    agent = _agent(db, org)
    opp = _opportunity(db)
    application = _application(db, org, opp)

    first = _assemble(db, org, agent, opp, application, [_document(db, org, version=1)], ["cover_letter"])
    db.commit()
    second = _assemble(db, org, agent, opp, application, [_document(db, org, version=2)], ["cover_letter"])
    db.commit()

    assert first.fingerprint != second.fingerprint
    assert db.query(models.SubmissionPackage).count() == 2, (
        "a changed document was folded into the existing package, so the frozen authorisation no "
        "longer describes what would be sent"
    )


# ===========================================================================
# DEADLINE HONESTY
# ===========================================================================
def test_a_date_only_deadline_is_not_presented_as_midnight():
    """The manifest records whether a TIME was actually stated.

    A funder that publishes "31 March" has not said 00:00, and a system that treats it as midnight
    cuts off an application the funder would have accepted.
    """
    items = [PackageItem("d1", "cover_letter", "C", 1, "a" * 64, "k", "text/markdown")]
    deadline = datetime(2027, 3, 31, tzinfo=timezone.utc)
    assert not (deadline.hour or deadline.minute or deadline.second), "fixture is not date-only"

    org = models.Organisation(id="o", name="N", slug="n", owner_user_id="u")
    opportunity = models.Opportunity(
        id="p", title="T", source_url="https://e.org", source_name="F", country="NG",
        content_hash="a" * 64, dedupe_fingerprint="b" * 64, deadline=deadline,
    )
    application = models.Application(id="a", org_id="o", opportunity_id="p", state="PREPARING", version=1)

    manifest = packaging.build_manifest(
        organisation=org, opportunity=opportunity, application=application, items=items,
        missing=[], needs_data=[], status="DRAFT", assembled_at=datetime.now(timezone.utc),
    )
    assert manifest["opportunity"]["deadline_is_exact"] is False


def test_the_evidence_types_are_the_ones_an_organisation_must_upload():
    """The split that decides whether a gap is reported as the NGO's action or the engine's."""
    assert "registration_certificate" in EVIDENCE_TYPES
    assert "audited_accounts" in EVIDENCE_TYPES
    for generated in ("cover_letter", "budget", "workplan"):
        assert generated not in EVIDENCE_TYPES, (
            f"{generated} is generated by the platform, so its absence is not the organisation's to "
            "close"
        )
