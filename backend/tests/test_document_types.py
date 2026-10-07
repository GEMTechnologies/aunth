"""The document-type registry, and the defect that made readiness unsatisfiable.

THE DEFECT
----------
`workspace.readiness()` required `doc_type == "audited_accounts"`. Nothing in the codebase
produced that type: every helper and fixture creates `audited_financial_statements`. There
was no canonical list anywhere.

So an organisation that uploaded its audited accounts under the product's own name was told
**"a audited accounts is required but not held"** - forever - and could never submit to any
funder whose listing mentioned audited accounts.

It survived because every existing test opportunity's eligibility text omitted those phrases,
so the branch never executed. A journey test walking a realistic listing found it in one run.
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
from agent import document_types  # noqa: E402
from agent.organisation_memory import (  # noqa: E402
    DocumentVault,
    OrganisationMemory,
    checksum_bytes,
)
from agent.opportunity_ingestion import (  # noqa: E402
    OpportunityIngestionAdapter,
    RawOpportunity,
)
from agent.workspace import ApplicationWorkspace  # noqa: E402


# ===========================================================================
# THE REGISTRY
# ===========================================================================
def test_the_two_names_for_audited_accounts_mean_the_same_thing():
    """THE regression guard. These are the two names that drifted apart."""
    assert document_types.is_same_type("audited_accounts", "audited_financial_statements")
    assert document_types.canonical("audited_financial_statements") == "audited_accounts"
    assert document_types.canonical("audited_accounts") == "audited_accounts"


def test_audited_and_unaudited_are_NOT_the_same():
    """Exact after canonicalisation, not substring matching.

    A gate that confused these would accept an unaudited statement where an audited one was
    required - which is worse than a blocker, because it looks like compliance.
    """
    assert not document_types.is_same_type("audited_accounts", "unaudited_accounts")
    assert not document_types.is_same_type("tax_clearance", "tax_clearance_draft")


def test_an_unrecognised_type_equals_only_itself():
    """A deployment that invents a type must not have it silently merged with a known one."""
    assert document_types.is_same_type("custom_doc", "custom_doc")
    assert not document_types.is_same_type("custom_doc", "registration_certificate")
    assert document_types.canonical("custom_doc") is None


def test_every_canonical_name_maps_to_itself():
    """So the registry is internally consistent and `canonical` is idempotent."""
    for name in document_types.known_types():
        assert document_types.canonical(name) == name


def test_the_requirement_phrases_name_types_that_the_vault_can_hold():
    """The check that would have caught the defect: every type the GATE demands must be a
    type the REGISTRY declares. A requirement with no producible type is a permanent
    blocker."""
    for doc_type in document_types.REQUIREMENT_PHRASES:
        assert doc_type in document_types.known_types(), (
            f"the readiness gate requires {doc_type!r}, which is not a declared document "
            "type - so it can never be satisfied"
        )


def test_a_listing_mentioning_audited_accounts_requires_that_type():
    required = document_types.required_types_for(
        "Registered NGOs in Uganda with audited accounts."
    )
    assert "audited_accounts" in required


def test_a_listing_mentioning_nothing_requires_nothing():
    """So the gate does not demand documents from a listing that never asked."""
    assert document_types.required_types_for("A grant for child protection work.") == []
    assert document_types.required_types_for(None) == []


def test_the_requirement_order_is_stable():
    """A readiness report whose blockers reorder between calls is one nobody can diff."""
    text = "audited accounts, tax clearance, bank details, registration certificate"
    first = document_types.required_types_for(text)
    for _ in range(5):
        assert document_types.required_types_for(text) == first


# ===========================================================================
# THE GATE, ON A REAL LISTING
# ===========================================================================
@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "doctypes.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def org(db):
    """A verified Ugandan NGO, ready for readiness to have an opinion about documents."""
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()

    organisation = models.Organisation(
        id=str(uuid.uuid4()), name="War Child Test",
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(organisation)
    db.commit()

    memory = OrganisationMemory(db, organisation.id)
    for key, value in (("country", "Uganda"), ("organisation_type", "NGO")):
        memory.record_fact(
            key=key, value=value, state=models.OrgFact.VERIFIED, source="user:1"
        )
    memory.record_fact(
        key="registration_valid_until", value="2035-01-01",
        state=models.OrgFact.VERIFIED, source="user:1",
        valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()
    return organisation


def _application_on(db, org, criteria: str):
    """Ingest a listing with the given eligibility text and open an application on it."""
    adapter = OpportunityIngestionAdapter(db)
    raw = RawOpportunity(
        title="Child Protection Grant 2027",
        source_url=f"https://unicef.org/{uuid.uuid4().hex[:10]}",
        source_name="UNICEF",
        country="Uganda",
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        description="Funding.",
        deadline=datetime.now(timezone.utc) + timedelta(days=60),
        eligibility_criteria=criteria,
        is_active=True,
        scraped_at=datetime.now(timezone.utc),
    )
    adapter.ingest(raw)
    db.commit()

    opportunity = db.query(models.Opportunity).filter(
        models.Opportunity.source_url == raw.source_url
    ).one()
    workspace = ApplicationWorkspace(db, org.id)
    application = workspace.create(opportunity)
    db.commit()
    return workspace, application


def test_a_listing_requiring_audited_accounts_is_SATISFIABLE(db, org):
    """THE test the defect would have failed.

    An organisation holding an approved document named the way the product names it -
    `audited_financial_statements` - must satisfy a listing that asks for audited accounts.
    Before the registry, this was impossible, and the blocker read as missing evidence.
    """
    vault = DocumentVault(db, org.id)
    document = vault.add_version(
        title="Audited Financial Statements 2026",
        doc_type="audited_financial_statements",
        storage_key="org/x/afs.pdf",
        checksum_sha256=checksum_bytes(b"audited accounts"),
        mime_type="application/pdf",
        valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc),
    )
    vault.approve(document, approved_by=org.owner_user_id)
    db.commit()

    workspace, application = _application_on(
        db, org, "Registered NGOs in Uganda with audited accounts."
    )
    report = workspace.readiness(application)
    document_blockers = [b for b in report.blockers if "audited" in b.lower()]
    assert not document_blockers, (
        f"a listing requiring audited accounts is unsatisfiable although the organisation "
        f"holds approved audited financial statements: {document_blockers}"
    )


def test_the_blocker_lists_the_accepted_names(db, org):
    """Because "required but not held" for a document the organisation HAS under another
    name looks like missing evidence and is actually a vocabulary mismatch."""
    workspace, application = _application_on(
        db, org, "Registered NGOs in Uganda with audited accounts."
    )
    report = workspace.readiness(application)
    blocker = next(b for b in report.blockers if "audited" in b.lower())
    assert "accepted names" in blocker
    assert "audited_financial_statements" in blocker, (
        "the blocker does not name the alternative the organisation would have used"
    )


def test_an_unapproved_document_still_blocks(db, org):
    """The synonym tolerance must not weaken the approval requirement."""
    vault = DocumentVault(db, org.id)
    vault.add_version(
        title="Audited Financial Statements",
        doc_type="audited_financial_statements",
        storage_key="org/x/afs.pdf",
        checksum_sha256=checksum_bytes(b"x"),
        mime_type="application/pdf",
    )
    db.commit()   # added but NOT approved

    workspace, application = _application_on(
        db, org, "Registered NGOs in Uganda with audited accounts."
    )
    report = workspace.readiness(application)
    assert any("not approved" in b for b in report.blockers), (
        "an unapproved document satisfied the readiness gate"
    )
