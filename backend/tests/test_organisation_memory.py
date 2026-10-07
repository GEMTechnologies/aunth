"""Organisation Memory: the Digital Twin, and the rule that guards it.

The load-bearing test is ``test_ai_inferred_never_reaches_a_submission``. It is
the security gate's named failure: a model's guess silently becoming a fact in an
application a funder receives. It is silent by nature, and unrecoverable - a
submitted application cannot be un-submitted.
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

import models  # noqa: E402
from agent.organisation_memory import (  # noqa: E402
    ALL_STATES,
    SUBMISSION_SAFE_STATES,
    DocumentVault,
    FabricationRefused,
    OrganisationMemory,
    OrganisationMemoryError,
    UnknownFactState,
    checksum_bytes,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'twin.db'}", future=True)
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
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
        id=str(uuid.uuid4()), name="Test NGO", slug="test-ngo", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def other_org_factory(db):
    """A second tenant on the same session, for cross-tenant assertions.

    Deliberately the same session: tenant isolation under RLS is enforced by the
    database, and asserting it here would be asserting SQLite. What this fixture
    is for is the *application* boundary - that a service constructed for one
    organisation cannot reach another's rows through its own queries.
    """
    user = models.User(id=str(uuid.uuid4()), display_name="Other")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Other NGO", slug="other-ngo", owner_user_id=user.id
    )
    db.add(row)
    db.commit()
    return row.id, db


@pytest.fixture
def memory(db, org):
    return OrganisationMemory(db, org)


@pytest.fixture
def vault(db, org):
    return DocumentVault(db, org)


# ---------------------------------------------------------------------------
# THE rule
# ---------------------------------------------------------------------------
def test_ai_inferred_never_reaches_a_submission(db, org):
    """The security gate's named failure, tested from every direction.

    A model's inference must not appear in the mapping an agent writes from. It
    must not become submission-safe by being verified without a person. It must
    not become safe by being re-recorded. And the refusal must be loud at the
    point of use, not a missing key that a template silently renders blank.
    """
    memory = OrganisationMemory(db, org)
    memory.record_fact(
        key="annual_budget_usd",
        value=250000,
        state=models.OrgFact.AI_INFERRED,
        source="ai:model-gateway",
        confidence=0.62,
    )
    memory.record_fact(
        key="registration_number",
        value="NGO-1234",
        state=models.OrgFact.VERIFIED,
        source="user:abc",
    )
    db.commit()

    usable = memory.submission_facts()

    assert "registration_number" in usable
    assert "annual_budget_usd" not in usable, (
        "an AI-inferred fact reached the submission mapping"
    )

    # And the loud path refuses too, rather than returning None.
    with pytest.raises(FabricationRefused):
        memory.assert_submission_safe("annual_budget_usd")

    # The safe fact still resolves.
    assert memory.assert_submission_safe("registration_number").state == "VERIFIED"


def test_an_unknown_fact_is_refused_rather_than_defaulted(db, org):
    """A missing material fact becomes a user task, never a guess."""
    memory = OrganisationMemory(db, org)
    with pytest.raises(FabricationRefused) as excinfo:
        memory.assert_submission_safe("bank_account_iban")
    assert "user task" in str(excinfo.value)


def test_submission_safety_is_an_allow_list_not_a_deny_list():
    """A new state must default to UNSAFE.

    If the guard were a deny-list of ``AI_INFERRED``, adding a state later would
    silently make it submission-safe. That is the wrong direction for this
    mistake: an unclassified fact should fail closed.
    """
    assert models.OrgFact.AI_INFERRED in ALL_STATES
    assert models.OrgFact.AI_INFERRED not in SUBMISSION_SAFE_STATES
    assert models.OrgFact.EXPIRED not in SUBMISSION_SAFE_STATES
    # Everything safe is known, and everything known is classified.
    assert SUBMISSION_SAFE_STATES < ALL_STATES


def test_verification_requires_a_named_person(db, org):
    """"The system decided it was fine" is not verification."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(
        key="country", value="Uganda", state=models.OrgFact.AI_INFERRED, source="ai:model"
    )
    db.commit()

    with pytest.raises(OrganisationMemoryError):
        memory.verify_fact(key="country", verified_by="")
    with pytest.raises(OrganisationMemoryError):
        memory.verify_fact(key="country", verified_by=None)

    promoted = memory.verify_fact(key="country", verified_by="user-42")
    db.commit()
    assert promoted.state == models.OrgFact.VERIFIED
    assert promoted.verified_by == "user-42"
    assert "country" in memory.submission_facts()


def test_a_state_outside_the_closed_set_is_rejected(db, org):
    """Provenance is a property of the schema, not a convention."""
    memory = OrganisationMemory(db, org)
    with pytest.raises(UnknownFactState):
        memory.record_fact(key="x", value=1, state="PROBABLY_TRUE", source="user:1")


def test_a_fact_without_a_source_is_rejected(db, org):
    """"Nobody knows where this number came from" must not be representable."""
    memory = OrganisationMemory(db, org)
    with pytest.raises(OrganisationMemoryError) as excinfo:
        memory.record_fact(key="x", value=1, state=models.OrgFact.USER_PROVIDED, source="")
    assert "provenance" in str(excinfo.value)


def test_memory_refuses_an_unknown_tenant(db):
    """Tenant unknown is a deny, never a default tenant."""
    with pytest.raises(OrganisationMemoryError) as excinfo:
        OrganisationMemory(db, "")
    assert "deny" in str(excinfo.value)
    with pytest.raises(OrganisationMemoryError):
        DocumentVault(db, "")


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------
def test_recording_a_fact_appends_a_version_rather_than_overwriting(db, org):
    """An application submitted against version 3 stays explainable after 4."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="phone", value="+256700000001", state=models.OrgFact.USER_PROVIDED,
                       source="user:1")
    db.commit()
    memory.record_fact(key="phone", value="+256700000002", state=models.OrgFact.VERIFIED,
                       source="user:2")
    db.commit()

    history = memory.fact_history("phone")
    assert [f.version for f in history] == [1, 2]
    assert [f.is_current for f in history] == [False, True]
    assert history[0].value["value"] == "+256700000001"
    assert memory.current_fact("phone").value["value"] == "+256700000002"
    assert history[1].supersedes_id == history[0].id


def test_only_one_version_is_current(db, org):
    memory = OrganisationMemory(db, org)
    for value in ("a", "b", "c"):
        memory.record_fact(key="k", value=value, state=models.OrgFact.VERIFIED, source="user:1")
        db.commit()
    current = db.execute(
        select(models.OrgFact).where(
            models.OrgFact.org_id == org, models.OrgFact.key == "k",
            models.OrgFact.is_current.is_(True),
        )
    ).scalars().all()
    assert len(current) == 1
    assert current[0].version == 3


def test_version_numbers_are_unique_per_key(db, org):
    """Two concurrent writers must not both claim version 2."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="k", value="a", state=models.OrgFact.VERIFIED, source="user:1")
    db.commit()
    memory.record_fact(key="k", value="b", state=models.OrgFact.VERIFIED, source="user:1")
    db.commit()
    versions = [f.version for f in memory.fact_history("k")]
    assert versions == sorted(set(versions)), f"duplicate versions: {versions}"


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_an_expired_fact_is_not_submission_safe(db, org):
    """A lapsed certificate that still reads as current is how stale information
    reaches a funder."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(
        key="tax_clearance", value="TIN-9", state=models.OrgFact.VERIFIED,
        source="user:1", valid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.commit()

    assert "tax_clearance" not in memory.submission_facts()
    with pytest.raises(FabricationRefused):
        memory.assert_submission_safe("tax_clearance")


def test_a_future_expiry_is_still_submission_safe(db, org):
    """The control case: expiry must not block everything."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(
        key="tax_clearance", value="TIN-9", state=models.OrgFact.VERIFIED,
        source="user:1", valid_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.commit()
    assert memory.submission_facts()["tax_clearance"] == "TIN-9"


def test_expire_stale_facts_marks_them_and_reports_which(db, org):
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="old", value=1, state=models.OrgFact.VERIFIED, source="user:1",
                       valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    memory.record_fact(key="fresh", value=2, state=models.OrgFact.VERIFIED, source="user:1",
                       valid_until=datetime.now(timezone.utc) + timedelta(days=1))
    db.commit()

    expired = memory.expire_stale_facts()
    db.commit()
    assert expired == ["old"]
    assert memory.current_fact("old").state == models.OrgFact.EXPIRED
    assert memory.current_fact("fresh").state == models.OrgFact.VERIFIED


def test_naive_expiry_from_the_database_is_compared_safely(db, org):
    """``DateTime(timezone=True)`` is aware from PostgreSQL but naive from SQLite."""
    memory = OrganisationMemory(db, org)
    fact = memory.record_fact(
        key="cert", value="x", state=models.OrgFact.VERIFIED, source="user:1",
        valid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.commit()
    db.refresh(fact)
    assert memory.is_expired(fact) is True


# ---------------------------------------------------------------------------
# Missing facts as work items
# ---------------------------------------------------------------------------
def test_missing_facts_returns_work_items_rather_than_raising(db, org):
    """A caller that can only receive an exception will eventually fill the gap
    itself."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="country", value="Uganda", state=models.OrgFact.VERIFIED,
                       source="user:1")
    memory.record_fact(key="budget", value=1, state=models.OrgFact.AI_INFERRED, source="ai:m")
    db.commit()

    missing = {m.key: m.reason for m in memory.missing_facts(["country", "budget", "iban"])}
    assert "country" not in missing
    assert "not provided" in missing["iban"]
    assert "needs human confirmation" in missing["budget"]


def test_missing_facts_reports_expiry_distinctly(db, org):
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="cert", value=1, state=models.OrgFact.VERIFIED, source="user:1",
                       valid_until=datetime.now(timezone.utc) - timedelta(days=1))
    db.commit()
    assert memory.missing_facts(["cert"])[0].reason == "expired"
    assert memory.missing_facts(["cert"])[0].blocking is True


def test_inferred_facts_are_listed_for_review(db, org):
    """The moment a wrong inference is cheap to correct is before it is used."""
    memory = OrganisationMemory(db, org)
    memory.record_fact(key="a", value=1, state=models.OrgFact.AI_INFERRED, source="ai:m")
    memory.record_fact(key="b", value=2, state=models.OrgFact.VERIFIED, source="user:1")
    db.commit()
    assert [f.key for f in memory.inferred_facts()] == ["a"]


def test_one_tenants_twin_is_not_visible_to_another(db, memory, other_org_factory):
    """The application boundary, not the RLS boundary.

    This service is constructed with an org_id and must scope every query by it.
    If a query lost its ``org_id`` predicate the twins would merge, and the
    database would not necessarily catch it - the RLS tests cover that layer
    separately.
    """
    other_org, other_db = other_org_factory
    other_memory = OrganisationMemory(other_db, other_org)
    other_memory.record_fact(
        key="registration_number", value="THEIRS", state=models.OrgFact.VERIFIED,
        source="user:9",
    )
    db.commit()

    assert other_memory.submission_facts() == {"registration_number": "THEIRS"}
    assert memory.submission_facts() == {}, "this tenant saw the other tenant's fact"
    assert memory.current_fact("registration_number") is None


def test_current_facts_never_leak_across_tenants(db, memory, other_org_factory):
    other_org, other_db = other_org_factory
    other_memory = OrganisationMemory(other_db, other_org)
    for key in ("a", "b"):
        other_memory.record_fact(
            key=key, value=key, state=models.OrgFact.VERIFIED, source="user:9"
        )
    db.commit()
    assert memory.current_facts() == []
    assert [f.key for f in other_memory.current_facts()] == ["a", "b"]


# ---------------------------------------------------------------------------
# Document vault
# ---------------------------------------------------------------------------
def _doc(vault, **overrides):
    payload = {
        "title": "Certificate of Registration",
        "doc_type": "registration_certificate",
        "storage_key": "org/cert.pdf",
        "checksum_sha256": checksum_bytes(b"certificate-bytes"),
        "mime_type": "application/pdf",
        "size_bytes": 17,
    }
    payload.update(overrides)
    return vault.add_version(**payload)


def test_an_uploaded_document_is_not_usable_until_approved(vault):
    """Uploading is not approving, and the gap is where the wrong document gets
    attached to a real application."""
    document = _doc(vault)
    assert document.approval_status == models.Document.PENDING
    assert vault.usable() == []


def test_approval_makes_a_document_usable(vault):
    document = _doc(vault)
    vault.approve(document, approved_by="user-1")
    vault.db.commit()
    assert [d.id for d in vault.usable()] == [document.id]


def test_approval_requires_a_named_person(vault):
    document = _doc(vault)
    with pytest.raises(OrganisationMemoryError):
        vault.approve(document, approved_by="")


def test_a_document_needs_a_real_checksum(vault):
    """A receipt naming a document id is only meaningful if the bytes cannot
    change."""
    with pytest.raises(OrganisationMemoryError) as excinfo:
        _doc(vault, checksum_sha256="")
    assert "checksum" in str(excinfo.value)
    with pytest.raises(OrganisationMemoryError):
        _doc(vault, checksum_sha256="tooshort")


def test_a_new_version_is_pending_even_when_the_old_one_was_approved(vault):
    """The new file has different bytes, and it was the bytes that were approved."""
    first = _doc(vault)
    vault.approve(first, approved_by="user-1")
    vault.db.commit()

    second = _doc(vault, checksum_sha256=checksum_bytes(b"different-bytes"))
    vault.db.commit()

    assert second.version == 2
    assert second.approval_status == models.Document.PENDING
    assert vault.usable() == [], "a superseded approval made an unapproved file usable"
    assert first.is_current is False


def test_approving_a_superseded_version_is_refused(vault):
    """Approving superseded bytes is a silent mismatch between what was approved
    and what would be attached."""
    first = _doc(vault)
    _doc(vault, checksum_sha256=checksum_bytes(b"newer"))
    vault.db.commit()
    with pytest.raises(OrganisationMemoryError) as excinfo:
        vault.approve(first, approved_by="user-1")
    assert "no longer current" in str(excinfo.value)


def test_an_expired_document_is_not_usable(vault):
    document = _doc(vault, valid_until=datetime.now(timezone.utc) - timedelta(days=1))
    vault.approve(document, approved_by="user-1")
    vault.db.commit()
    assert vault.usable() == []


def test_expiring_soon_warns_before_a_submission_goes_out(vault):
    """The point of the vault knowing about expiry."""
    soon = _doc(vault, storage_key="a.pdf", valid_until=datetime.now(timezone.utc) + timedelta(days=5))
    far = _doc(vault, storage_key="b.pdf", valid_until=datetime.now(timezone.utc) + timedelta(days=365))
    for document in (soon, far):
        vault.approve(document, approved_by="user-1")
    vault.db.commit()

    assert [d.storage_key for d in vault.expiring_soon(days=30)] == ["a.pdf"]


def test_usable_filters_by_type_and_scope(vault):
    org_doc = _doc(vault, storage_key="org.pdf")
    project_doc = _doc(
        vault, storage_key="proj.pdf", doc_type="audit_report",
        scope=models.Document.SCOPE_PROJECT, scope_ref="proj-1",
    )
    for document in (org_doc, project_doc):
        vault.approve(document, approved_by="user-1")
    vault.db.commit()

    assert [d.storage_key for d in vault.usable(doc_type="audit_report")] == ["proj.pdf"]
    assert [d.storage_key for d in vault.usable(
        scope=models.Document.SCOPE_PROJECT, scope_ref="proj-1")] == ["proj.pdf"]
    assert vault.usable(scope=models.Document.SCOPE_PROJECT, scope_ref="proj-2") == []


def test_a_project_scoped_document_must_name_its_project(vault):
    """Attaching one project's audit to another project's application is a real
    error, and the schema should make it expressible rather than inferable."""
    with pytest.raises(OrganisationMemoryError):
        _doc(vault, scope=models.Document.SCOPE_PROJECT, scope_ref=None)


def test_an_unknown_scope_is_rejected(vault):
    with pytest.raises(OrganisationMemoryError):
        _doc(vault, scope="WHATEVER")


def test_a_rejected_document_is_not_usable(vault):
    document = _doc(vault)
    vault.reject(document, rejected_by="user-1")
    vault.db.commit()
    assert vault.usable() == []


def test_assert_usable_refuses_a_pending_document(vault):
    document = _doc(vault)
    vault.db.commit()
    with pytest.raises(OrganisationMemoryError) as excinfo:
        vault.assert_usable(document.id)
    assert "uploading is" in str(excinfo.value)


def test_assert_usable_refuses_another_organisations_document(db, vault, other_org_factory):
    """Their document is APPROVED, so tenancy is the only thing that can stop it.

    That detail is the whole test. The first version left their document
    ``PENDING``, and the approval check refused it before the tenancy check was
    ever reached - so removing the ``org_id`` predicate did not fail the test.
    Approving it first means the organisation filter is the only remaining
    barrier, which is what the assertion claims to be about.
    """
    other_org, other_db = other_org_factory
    other_vault = DocumentVault(other_db, other_org)
    theirs = other_vault.add_version(
        title="Theirs", doc_type="x", storage_key="t.pdf",
        checksum_sha256=checksum_bytes(b"theirs"), mime_type="application/pdf",
    )
    other_vault.approve(theirs, approved_by="user-9")
    db.commit()
    assert theirs.approval_status == models.Document.APPROVED

    with pytest.raises(OrganisationMemoryError) as excinfo:
        vault.assert_usable(theirs.id)
    assert "no document" in str(excinfo.value)


def test_checksum_is_stable_and_content_addressed():
    assert checksum_bytes(b"abc") == checksum_bytes(b"abc")
    assert checksum_bytes(b"abc") != checksum_bytes(b"abd")
    assert len(checksum_bytes(b"abc")) == 64
