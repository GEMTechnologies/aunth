"""Package readiness: the directive's `evaluatePackageReadiness`.

THE DISTINCTION THIS FILE EXISTS TO PROTECT

    A missing registration certificate is not an infrastructure error.
    A successful package assembly is not a successful application submission.

Readiness is evaluated at READ time from persisted state, not stored, so a package assembled while an
opportunity was open cannot keep reporting READY after the deadline passed.
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
from agent import readiness  # noqa: E402
from agent.readiness import ASSEMBLING, BLOCKED, FAILED, READY, evaluate  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "readiness.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _opportunity(db, *, days=30, is_active=True, exact_time=True):
    deadline = datetime.now(timezone.utc) + timedelta(days=days)
    if not exact_time:
        deadline = deadline.replace(hour=0, minute=0, second=0, microsecond=0)
    row = models.Opportunity(
        id=str(uuid.uuid4()), title="Health Grant", source_url="https://f.example/x",
        source_name="Funder", country="Nigeria", content_hash="a" * 64,
        dedupe_fingerprint="b" * 64, is_active=is_active, deadline=deadline,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def _agent_for(db, org_id):
    """A persisted agent for the organisation. Not a random id - the column is a foreign key."""
    row = models.GranadaAgent(
        id=str(uuid.uuid4()), org_id=org_id, display_name="Agent",
        status=models.GranadaAgent.ACTIVE,
    )
    db.add(row)
    db.commit()
    return str(row.id)


def _package(db, opportunity, *, status=None, manifest=None, documents=None, missing=None,
             needs_data=None, deadline_exact=True):
    org_id = str(uuid.uuid4())
    # The organisation's owner must exist: `owner_user_id` is a foreign key, and a package with no
    # owning user is not a state production can reach.
    owner_id = str(uuid.uuid4())
    db.add(models.User(id=owner_id, display_name="Owner"))
    db.commit()
    db.add(models.Organisation(id=org_id, name="N", slug=f"o-{uuid.uuid4().hex[:6]}",
                               owner_user_id=owner_id))
    db.commit()
    docs = documents if documents is not None else [{"doc_type": "cover_letter", "version": 1,
                                                     "checksum_sha256": "c" * 64}]
    manifest = manifest or {
        "opportunity": {
            "id": str(opportunity.id), "title": opportunity.title,
            "deadline": opportunity.deadline.isoformat() if opportunity.deadline else None,
            "deadline_is_exact": deadline_exact,
        },
        "documents": docs,
        "missing_requirements": missing or [],
        "needs_data": needs_data or [],
        "document_count": len(docs),
    }
    row = models.SubmissionPackage(
        id=str(uuid.uuid4()), org_id=org_id,
        # A real agent row: `agent_id` is a foreign key, and a package authored by no agent is not a
        # state production can reach.
        agent_id=_agent_for(db, org_id),
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
    row._opportunity = opportunity
    return row


# ===========================================================================
# READY
# ===========================================================================
def test_a_complete_package_for_an_open_opportunity_is_READY(db):
    opportunity = _opportunity(db)
    package = _package(db, opportunity)
    verdict = evaluate(package)
    assert verdict.verdict == READY, verdict.blockers
    assert verdict.is_ready
    assert verdict.satisfied == 1


def test_a_READY_package_does_NOT_claim_it_was_submitted(db):
    """THE directive's bad example: "Application completed" when only preparation succeeded."""
    opportunity = _opportunity(db)
    message = evaluate(_package(db, opportunity)).message()
    assert "ready" in message.lower()
    assert "not yet submitted" in message.lower()
    assert "submitted" not in message.lower().replace("not yet submitted", "")


# ===========================================================================
# BLOCKED vs FAILED - the distinction that matters most
# ===========================================================================
def test_a_missing_evidence_document_is_BLOCKED_and_needs_the_organisation(db):
    """A registration certificate the NGO has not uploaded is the organisation's action. Reporting it
    as a technical failure would page an engineer for a missing upload."""
    opportunity = _opportunity(db)
    package = _package(db, opportunity, status=models.SubmissionPackage.NEEDS_DATA,
                       missing=["registration_certificate"], needs_data=["registration_certificate"])
    verdict = evaluate(package)

    assert verdict.verdict == BLOCKED
    assert verdict.verdict != FAILED, "an organisation gap was reported as a technical failure"
    assert verdict.organisation_actions
    assert "registration certificate" in verdict.organisation_actions[0]
    assert not verdict.platform_actions


def test_a_generated_document_that_is_absent_is_the_PLATFORM_gap(db):
    """The inverse. `cover_letter` is produced by the platform, so its absence must not be reported
    as something the organisation must upload."""
    opportunity = _opportunity(db)
    package = _package(db, opportunity, missing=["cover_letter"], needs_data=[])
    verdict = evaluate(package)

    assert verdict.verdict == BLOCKED
    assert verdict.platform_actions, "the platform's own gap was not reported as the platform's"
    assert not verdict.organisation_actions, (
        "the organisation was asked to provide something the platform generates"
    )


def test_a_technical_failure_is_FAILED_not_BLOCKED(db):
    opportunity = _opportunity(db)
    package = _package(db, opportunity, status=models.SubmissionPackage.FAILED_FINAL)
    verdict = evaluate(package)
    assert verdict.verdict == FAILED
    assert verdict.blockers[0].code == "TECHNICAL_FAILURE"


def test_the_two_verdicts_are_reachable_and_distinguishable(db):
    """A guard that cannot tell them apart is useless, so both must be produced by real states."""
    opportunity = _opportunity(db)
    blocked = evaluate(_package(db, opportunity, missing=["tax_clearance"],
                                needs_data=["tax_clearance"]))
    failed = evaluate(_package(db, opportunity, status=models.SubmissionPackage.FAILED_FINAL))
    assert blocked.verdict != failed.verdict


# ===========================================================================
# DEADLINE
# ===========================================================================
def test_an_EXPIRED_opportunity_is_BLOCKED(db):
    """A technically complete package for a closed opportunity must not be presented as
    submittable."""
    opportunity = _opportunity(db, days=-1)
    verdict = evaluate(_package(db, opportunity))
    assert verdict.verdict == BLOCKED
    assert any(b.code == "OPPORTUNITY_CLOSED" for b in verdict.blockers)


def test_a_date_only_deadline_WARNS_without_blocking(db):
    """A funder that publishes "31 March" has not stated 00:00. Treating it as midnight would cut off
    an application the funder would have accepted, so it is a warning an operator sees - not a
    refusal."""
    opportunity = _opportunity(db)
    verdict = evaluate(_package(db, opportunity, deadline_exact=False))

    assert verdict.verdict == READY, "a date-only deadline blocked a package it should not"
    assert any(b.code == "DEADLINE_DATE_ONLY" for b in verdict.blockers), (
        "the date-only caveat was not surfaced at all"
    )


def test_an_INACTIVE_opportunity_is_BLOCKED(db):
    verdict = evaluate(_package(db, _opportunity(db, is_active=False)))
    assert verdict.verdict == BLOCKED
    assert any(b.code == "OPPORTUNITY_INACTIVE" for b in verdict.blockers)


# ===========================================================================
# SUPERSEDED AND IN-FLIGHT
# ===========================================================================
def test_a_SUPERSEDED_package_is_not_ready(db):
    """A newer revision replaced it, so the authorisation a human gave applied to a different set."""
    opportunity = _opportunity(db)
    package = _package(db, opportunity, status=models.SubmissionPackage.SUPERSEDED)
    verdict = evaluate(package)
    assert verdict.verdict == BLOCKED
    assert any(b.code == "SUPERSEDED" for b in verdict.blockers)


def test_a_package_mid_submission_is_ASSEMBLING(db):
    opportunity = _opportunity(db)
    package = _package(db, opportunity, status=models.SubmissionPackage.SUBMITTING)
    assert evaluate(package).verdict == ASSEMBLING


# ===========================================================================
# NO MATERIAL
# ===========================================================================
def test_a_package_with_no_documents_is_not_ready(db):
    opportunity = _opportunity(db)
    package = _package(db, opportunity, documents=[])
    verdict = evaluate(package)
    assert verdict.verdict == BLOCKED
    assert any(b.code == "NO_DOCUMENTS" for b in verdict.blockers)


# ===========================================================================
# THE MESSAGE
# ===========================================================================
def test_the_message_names_the_blocker(db):
    """"Package failed" tells nobody what to do next - the directive says so explicitly."""
    opportunity = _opportunity(db)
    package = _package(db, opportunity, missing=["audited_accounts"],
                       needs_data=["audited_accounts"])
    message = evaluate(package).message()
    assert "audited accounts" in message.lower()
    assert "blocked" in message.lower()


def test_the_message_counts_on_success(db):
    """The directive's good example: "Package ready: 8 of 8 mandatory requirements satisfied."."""
    opportunity = _opportunity(db)
    message = evaluate(_package(db, opportunity)).message()
    assert "1 of 1" in message


# ===========================================================================
# THE API SHAPE
# ===========================================================================
def test_as_dict_is_json_serialisable(db):
    opportunity = _opportunity(db)
    import json

    json.dumps(evaluate(_package(db, opportunity)).as_dict())


def test_evaluate_by_id_returns_None_for_an_unknown_package(db):
    """`None` rather than an exception: a caller reporting to a user distinguishes "no such package"
    from "this package is blocked", and an exception forces both into one shape."""
    assert readiness.evaluate_by_id(db, str(uuid.uuid4())) is None


def test_evaluate_by_id_loads_the_opportunity_itself(db):
    """Readiness needs the opportunity but must not mutate the package row to get it - evaluating
    is a READ."""
    opportunity = _opportunity(db)
    package = _package(db, opportunity)
    verdict = readiness.evaluate_by_id(db, str(package.id))
    assert verdict is not None
    assert verdict.verdict == READY
