"""Requirement drift: a package frozen against a listing that has since changed.

`SubmissionPackage` freezes documents by checksum. Nothing compared the LISTING - so a funder could add
a mandatory attachment or move a deadline, and the package kept its fingerprint and went on reporting
itself ready. The directive's §13.
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
from agent import requirement_drift as drift  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "drift.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db):
    row = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(row)
    db.commit()
    return row


def _org(db):
    row = models.Organisation(
        id=str(uuid.uuid4()), name="N", slug=f"o-{uuid.uuid4().hex[:6]}",
        owner_user_id=_user(db).id,
    )
    db.add(row)
    db.commit()
    return row


def _agent(db, org_id):
    row = models.GranadaAgent(
        id=str(uuid.uuid4()), org_id=org_id, display_name="A", status=models.GranadaAgent.ACTIVE,
    )
    db.add(row)
    db.commit()
    return row


def _opportunity(db, *, days=30, title="Community Health Grant", description="",
                 country="Nigeria"):
    row = models.Opportunity(
        id=str(uuid.uuid4()), title=title, description=description, source_url="https://f.example/x",
        source_name="Funder", country=country, content_hash="a" * 64, dedupe_fingerprint="b" * 64,
        is_active=True, deadline=datetime.now(timezone.utc) + timedelta(days=days),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def _package(db, org, opportunity, *, required, documents=None, status=None, deadline=None):
    manifest = {
        "opportunity": {
            "id": str(opportunity.id), "title": opportunity.title,
            "deadline": (deadline or opportunity.deadline).isoformat(),
        },
        "documents": [
            {"doc_type": t, "version": 1, "checksum_sha256": "c" * 64} for t in (documents or [])
        ],
        "missing_requirements": sorted(set(required) - set(documents or [])),
        "required_types": sorted(required),
        "document_count": len(documents or []),
    }
    row = models.SubmissionPackage(
        id=str(uuid.uuid4()), org_id=org.id, agent_id=_agent(db, org.id).id,
        application_id=str(uuid.uuid4()), opportunity_id=str(opportunity.id),
        package_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        manifest=manifest, application_version=1,
        status=status or models.SubmissionPackage.DRAFT,
        submission_mode=models.SubmissionPackage.MODE_HANDOFF,
        idempotency_key=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


# ===========================================================================
# NO DRIFT
# ===========================================================================
def test_an_unchanged_listing_reports_NO_drift(db):
    """The common case, and it must be quiet. A detector that always fires is one nobody reads."""
    org = _org(db)
    opportunity = _opportunity(db)
    package = _package(db, org, opportunity,
                       required=["cover_letter", "organisation_profile"])
    report = drift.detect(package, opportunity)

    assert not report.drifted
    assert report.changes == []
    assert "unchanged" in report.message()


# ===========================================================================
# DRIFT THAT INVALIDATES WORK
# ===========================================================================
def test_a_NEW_mandatory_requirement_is_drift(db):
    """A funder adding an attachment means the package no longer satisfies the listing."""
    org = _org(db)
    opportunity = _opportunity(db, description="Applicants must submit a safeguarding policy.")
    package = _package(db, org, opportunity, required=["cover_letter"])

    report = drift.detect(package, opportunity)
    assert report.drifted
    assert any(c.kind == "REQUIREMENTS_ADDED" for c in report.changes)
    assert report.material_changes, "an added requirement did not invalidate existing work"


def test_a_MOVED_EARLIER_deadline_is_drift(db):
    org = _org(db)
    opportunity = _opportunity(db, days=10)
    package = _package(db, org, opportunity, required=["cover_letter"], deadline=opportunity.deadline)
    opportunity.deadline = datetime.now(timezone.utc) + timedelta(days=2)
    db.commit()

    report = drift.detect(package, opportunity)
    assert any(c.kind == "DEADLINE_MOVED" for c in report.changes)
    assert "earlier" in report.message()


def test_a_WITHDRAWN_deadline_is_drift(db):
    """The listing stopped stating a date, so the stored one is no longer supported."""
    org = _org(db)
    opportunity = _opportunity(db)
    package = _package(db, org, opportunity, required=["cover_letter"])
    opportunity.deadline = None
    db.commit()

    report = drift.detect(package, opportunity)
    assert any(c.kind == "DEADLINE_WITHDRAWN" for c in report.changes)


# ===========================================================================
# DRIFT THAT DOES *NOT* INVALIDATE WORK
# ===========================================================================
def test_a_REMOVED_requirement_does_NOT_invalidate_work(db):
    """The package holds a document the funder no longer asks for.

    That is untidy, not wrong. Reporting it as a blocker would send an organisation back to redo work
    for no reason - so it is surfaced without being material.
    """
    org = _org(db)
    opportunity = _opportunity(db)
    package = _package(
        db, org, opportunity, required=["cover_letter", "organisation_profile", "safeguarding_policy"],
        documents=["cover_letter", "organisation_profile", "safeguarding_policy"],
    )
    report = drift.detect(package, opportunity)
    assert report.drifted, "a removed requirement should still be surfaced"
    assert not report.material_changes, "a removed requirement blocked the package"
    assert any(c.kind == "REQUIREMENTS_REMOVED" for c in report.changes)


# ===========================================================================
# NOT DRIFT
# ===========================================================================
def test_a_missing_but_unchanged_requirement_is_NOT_drift(db):
    """Missing material is readiness's question, not drift's. Conflating them would make every
    incomplete package also look stale, and the remedies differ."""
    org = _org(db)
    opportunity = _opportunity(db)
    package = _package(db, org, opportunity,
                       required=["cover_letter", "organisation_profile"], documents=[])
    report = drift.detect(package, opportunity)
    assert not report.drifted, "a missing document was reported as requirement drift"


def test_a_deadline_passing_is_NOT_drift(db):
    """That is readiness (`OPPORTUNITY_CLOSED`), and a package does not drift merely because time
    passed."""
    org = _org(db)
    opportunity = _opportunity(db, days=-1)
    package = _package(db, org, opportunity,
                       required=["cover_letter", "organisation_profile"],
                       documents=["cover_letter", "organisation_profile"])
    report = drift.detect(package, opportunity)
    assert not report.drifted


def test_an_unparseable_stored_deadline_is_NOT_drift(db):
    """A formatting problem in stored JSON must not make every package look stale."""
    org = _org(db)
    opportunity = _opportunity(db)
    package = _package(db, org, opportunity, required=["cover_letter"])
    package.manifest["opportunity"]["deadline"] = "not-a-date"
    db.commit()

    report = drift.detect(package, opportunity)
    assert not any(c.kind.startswith("DEADLINE") for c in report.changes)


# ===========================================================================
# RECORDING, NEVER APPLYING
# ===========================================================================
def test_detect_CHANGES_NOTHING(db):
    """Detection reads and reports. A caller decides, and the safe default is to do nothing but tell
    somebody."""
    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(db, org, opportunity, required=["cover_letter"])
    before_status, before_reason = package.status, package.status_reason

    drift.detect(package, opportunity)
    assert package.status == before_status
    assert package.status_reason == before_reason


def test_mark_needs_review_records_the_reason(db):
    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(db, org, opportunity, required=["cover_letter"])

    report = drift.detect(package, opportunity)
    assert drift.mark_needs_review(db, package, report) is True
    assert package.status == models.SubmissionPackage.NEEDS_DATA
    assert "changed since" in package.status_reason


def test_mark_needs_review_is_IDEMPOTENT(db):
    """Re-running detection must not churn the row - the lesson from the ingestion counters."""
    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(db, org, opportunity, required=["cover_letter"])

    report = drift.detect(package, opportunity)
    assert drift.mark_needs_review(db, package, report) is True
    assert drift.mark_needs_review(db, package, report) is False, "a second call changed the row again"


def test_an_AUTHORISED_package_is_NOT_automatically_revoked(db):
    """A person authorised a specific frozen set. A later listing change must not silently undo a
    human decision - that needs a human."""
    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(
        db, org, opportunity, required=["cover_letter"],
        status=models.SubmissionPackage.AUTHORISED,
    )

    report = drift.detect(package, opportunity)
    assert report.drifted
    assert drift.mark_needs_review(db, package, report) is False
    assert package.status == models.SubmissionPackage.AUTHORISED, (
        "drift revoked a human authorisation without asking"
    )


def test_a_TERMINAL_package_is_left_alone(db):
    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(
        db, org, opportunity, required=["cover_letter"],
        status=models.SubmissionPackage.SUBMITTED,
    )
    report = drift.detect(package, opportunity)
    assert drift.mark_needs_review(db, package, report) is False


# ===========================================================================
# SNAPSHOT IDENTITY
# ===========================================================================
def test_the_snapshot_digest_ignores_ORDERING():
    """A set that merely reordered is not a change."""
    a = drift.RequirementSnapshot(frozenset({"a", "b"}), None, None, frozenset())
    b = drift.RequirementSnapshot(frozenset({"b", "a"}), None, None, frozenset())
    assert a.digest() == b.digest()


def test_detect_by_id_returns_None_for_an_unknown_package(db):
    assert drift.detect_by_id(db, str(uuid.uuid4())) is None


def test_the_report_is_json_serialisable(db):
    import json

    org = _org(db)
    opportunity = _opportunity(db, description="A safeguarding policy is required.")
    package = _package(db, org, opportunity, required=["cover_letter"])
    json.dumps(drift.detect(package, opportunity).as_dict())
