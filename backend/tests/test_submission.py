"""Phase 8: the submission pipeline and the four things that must never happen.

The tests are organised around guarantees rather than around methods, because the
failure modes here are not crashes:

* a submission filed with **no human authorisation**;
* a **second filing** because a response was lost;
* ``SUBMITTED`` recorded **without a receipt**;
* an adapter permitted only to **read** a portal filing through it.

Each would pass a happy-path test and each is a real incident. Filing twice is the worst
of them: many programmes disqualify **both** bids from an organisation that appears to
have submitted twice, so an uncertainty bug does not merely duplicate work, it can cost
the grant outright.

**Production submissions remain zero.** No adapter that can file exists outside this
file's fakes.
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
from agent.mail.ceiling import (  # noqa: E402
    Capability,
    CapabilityRefused,
    assert_capability,
)
from agent.submission.contract import (  # noqa: E402
    FrozenAnswer,
    FrozenDocument,
    SubmissionFailure,
    SubmissionOutcome,
)
from agent.submission.providers.fake import (  # noqa: E402
    READ_ONLY,
    FakeHandoffBuilder,
    FakeReadOnlySubmissionProvider,
    FakeSubmissionProvider,
)
from agent.submission.service import (  # noqa: E402
    NotAuthorisable,
    SubmissionError,
    SubmissionService,
    diff_fingerprint_inputs,
    package_fingerprint,
)
from tests.test_mail import _opportunity_and_application, _org_and_agent  # noqa: E402


# ===========================================================================
# FIXTURES
# ===========================================================================
@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "submission.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def world(db):
    """An organisation, its agent, and a ready-to-submit application."""
    org, agent_service = _org_and_agent(db, with_documents=True)
    agent = agent_service.get()
    _opportunity, application = _opportunity_and_application(db, org)
    return org, agent, application


def _service(db, world, provider=None, handoff_builder=None):
    org, agent, _application = world
    return SubmissionService(
        db, org_id=org.id, agent_id=agent.id,
        provider=provider, handoff_builder=handoff_builder,
    )


def _answers():
    return [
        {"question": "What is your organisation's legal name?",
         "answer": "War Child Test", "source": "org_fact:organisation_name", "verified": True},
        {"question": "Describe your safeguarding policy.",
         "answer": "We follow the Uganda national safeguarding framework.",
         "source": "model:draft", "verified": False},
    ]


def _budget():
    return {"currency": "UGX", "total": 120_000_000, "lines": [
        {"item": "staff", "amount": 80_000_000}, {"item": "materials", "amount": 40_000_000}]}


def _frozen_package(db, world, *, mode=models.SubmissionPackage.MODE_ADAPTER, **overrides):
    """A frozen, authorised package ready to file."""
    org, agent, application = world
    service = _service(db, world)
    kwargs = {
        "application": application,
        "documents": [],
        "answers": _answers(),
        "budget": _budget(),
        "contact_email": "grants@warchild.org",
        "target_url": "https://funder.example.org/apply",
        "mode": mode,
    }
    kwargs.update(overrides)
    package = service.build_package(**kwargs)
    db.commit()
    return service, package


def _authorise(db, world, package):
    from agent.workspace import ApplicationWorkspace

    org, agent, application = world
    service = SubmissionService(db, org_id=org.id, agent_id=agent.id)
    # The owner authorises. `has_permission` checks ownership before membership, so the
    # organisation's own owner is permitted.
    package = service.authorise(package_id=package.id, user_id=org.owner_user_id)
    db.commit()
    return service, package


# ===========================================================================
# THE FINGERPRINT: WHAT A HUMAN ACTUALLY AUTHORISES
# ===========================================================================
def test_the_same_artefacts_produce_the_same_fingerprint():
    """Re-freezing identical artefacts must not look like a change, or every re-read
    would invalidate an authorisation and nothing could ever be filed."""
    arguments = dict(
        org_id="org-1", agent_id="agent-1", application_id="app-1",
        documents=[FrozenDocument(document_id="d1", version=1, checksum_sha256="abc")],
        answers=[FrozenAnswer(question="Q", answer="A", source="fact", verified=True)],
        budget={"total": 100}, contact_email="a@b.org", target_url="https://x",
    )
    assert package_fingerprint(**arguments)[0] == package_fingerprint(**arguments)[0]


def test_document_order_is_not_a_change():
    """A form that lists documents in a different order has not changed what will be
    filed, and invalidating an authorisation over it would be noise."""
    docs = [
        FrozenDocument(document_id="d1", version=1, checksum_sha256="aaa"),
        FrozenDocument(document_id="d2", version=1, checksum_sha256="bbb"),
    ]
    common = dict(
        org_id="o", agent_id="a", application_id="p", answers=[],
        budget={}, contact_email=None, target_url=None,
    )
    assert package_fingerprint(documents=docs, **common)[0] == \
        package_fingerprint(documents=list(reversed(docs)), **common)[0]


def test_a_changed_document_checksum_changes_the_fingerprint():
    """The checksum is what proves the BYTES are the ones authorised. A re-upload that
    keeps the document id but changes the contents must invalidate the authorisation."""
    common = dict(
        org_id="o", agent_id="a", application_id="p", answers=[], budget={},
        contact_email=None, target_url=None,
    )
    before = package_fingerprint(
        documents=[FrozenDocument(document_id="d1", version=1, checksum_sha256="aaa")], **common
    )[0]
    after = package_fingerprint(
        documents=[FrozenDocument(document_id="d1", version=1, checksum_sha256="bbb")], **common
    )[0]
    assert before != after


def test_a_changed_document_version_changes_the_fingerprint():
    common = dict(
        org_id="o", agent_id="a", application_id="p", answers=[], budget={},
        contact_email=None, target_url=None,
    )
    before = package_fingerprint(
        documents=[FrozenDocument(document_id="d1", version=1, checksum_sha256="aaa")], **common
    )[0]
    after = package_fingerprint(
        documents=[FrozenDocument(document_id="d1", version=2, checksum_sha256="aaa")], **common
    )[0]
    assert before != after


def test_a_changed_answer_changes_the_fingerprint():
    """The organisation is accountable for every answer, so a changed answer must
    invalidate the authorisation. This is the difference between a fingerprint and a
    formality."""
    common = dict(
        org_id="o", agent_id="a", application_id="p", documents=[], budget={},
        contact_email=None, target_url=None,
    )
    before = package_fingerprint(
        answers=[FrozenAnswer(question="Q", answer="We do", source="fact", verified=True)], **common
    )[0]
    after = package_fingerprint(
        answers=[FrozenAnswer(question="Q", answer="We do not", source="fact", verified=True)], **common
    )[0]
    assert before != after


def test_answer_order_is_not_a_change_but_the_question_is():
    common = dict(
        org_id="o", agent_id="a", application_id="p", documents=[], budget={},
        contact_email=None, target_url=None,
    )
    a = FrozenAnswer(question="Q1", answer="A1", source="f", verified=True)
    b = FrozenAnswer(question="Q2", answer="A2", source="f", verified=True)
    assert package_fingerprint(answers=[a, b], **common)[0] == \
        package_fingerprint(answers=[b, a], **common)[0]
    changed = FrozenAnswer(question="Q1!", answer="A1", source="f", verified=True)
    assert package_fingerprint(answers=[changed, b], **common)[0] != \
        package_fingerprint(answers=[a, b], **common)[0]


def test_a_changed_budget_changes_the_fingerprint():
    """A transposed digit in a budget is exactly the error this must catch."""
    common = dict(
        org_id="o", agent_id="a", application_id="p", documents=[], answers=[],
        contact_email=None, target_url=None,
    )
    assert package_fingerprint(budget={"total": 100}, **common)[0] != \
        package_fingerprint(budget={"total": 1000}, **common)[0]
    # But an unrelated key reordering is not a change.
    assert package_fingerprint(budget={"a": 1, "b": 2}, **common)[0] == \
        package_fingerprint(budget={"b": 2, "a": 1}, **common)[0]


def test_an_answer_containing_the_separator_cannot_forge_a_field():
    """Length-prefixed, so a crafted answer cannot make two different packages hash the
    same - the classic canonicalisation bug."""
    common = dict(
        org_id="o", agent_id="a", application_id="p", documents=[], budget={},
        contact_email=None, target_url=None,
    )
    left = package_fingerprint(
        answers=[FrozenAnswer(question="Q", answer="A\x1fverified", source="x", verified=False)],
        **common,
    )[0]
    right = package_fingerprint(
        answers=[FrozenAnswer(question="Q", answer="A", source="x\x1fverified", verified=False)],
        **common,
    )[0]
    assert left != right


def test_the_diff_names_what_changed():
    """A refusal must be actionable, not merely a refusal."""
    common = dict(
        org_id="o", agent_id="a", application_id="p", documents=[], answers=[],
        budget={"total": 1}, contact_email=None, target_url=None,
    )
    before = package_fingerprint(**common)[1]
    after = package_fingerprint(**{**common, "budget": {"total": 2}})[1]
    difference = diff_fingerprint_inputs(before, after)
    assert difference["comparable"] is True
    assert any("budget" in str(item) for item in difference["changed"])


# ===========================================================================
# AUTHORISATION
# ===========================================================================
def test_a_package_is_created_awaiting_authorisation(db, world):
    _service_obj, package = _frozen_package(db, world)
    assert package.status == models.SubmissionPackage.AWAITING_AUTHORISATION
    assert package.package_fingerprint
    assert package.idempotency_key


def test_a_package_cannot_be_filed_without_an_authorisation(db, world):
    """The central guarantee. A filed application is a legally consequential statement
    to a funder made in the organisation's name."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)

    service = _service(db, world, provider=provider)
    run = service.execute(package_id=package.id)

    assert run.refused is True
    assert run.refusal_code == "NOT_AUTHORISED"
    assert provider.call_count == 0
    assert provider.submission_count == 0


def test_the_handoff_also_requires_an_authorisation(db, world):
    """Not an external action, and still gated: the bundle commits the organisation's
    evidence by naming the exact documents and answers to be filed in its name."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(
        db, world, mode=models.SubmissionPackage.MODE_HANDOFF
    )
    service = _service(db, world, provider=provider, handoff_builder=FakeHandoffBuilder())

    run = service.handoff(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "NOT_AUTHORISED"
    assert provider.submission_count == 0


def test_authorisation_requires_a_person_who_may_authorise(db, world):
    from agent.workspace import ApplicationWorkspace

    org, agent, application = world
    service = _service(db, world)
    package = service.build_package(
        application=application, answers=_answers(), budget=_budget(),
        target_url="https://x", mode=models.SubmissionPackage.MODE_ADAPTER,
    )
    db.commit()

    stranger = str(uuid.uuid4())
    with pytest.raises(SubmissionError):
        service.authorise(package_id=package.id, user_id=stranger)


def test_the_owner_may_authorise_their_own_submission(db, world):
    """Ownership precedes membership: an organisation's owner must be able to approve
    its own filings."""
    _service_obj, package = _frozen_package(db, world)
    _authorise(db, world, package)
    assert package.status == models.SubmissionPackage.AUTHORISED
    assert package.authorised_by


def test_an_authorised_package_can_be_frozen_again_unchanged(db, world):
    """Idempotent by (application, fingerprint), so a re-read does not create a second
    package for the same artefacts."""
    service, package = _frozen_package(db, world)
    org, agent, application = world
    again = service.build_package(
        application=application, answers=_answers(), budget=_budget(),
        contact_email="grants@warchild.org", target_url="https://funder.example.org/apply",
        mode=models.SubmissionPackage.MODE_ADAPTER,
    )
    db.commit()
    assert again.id == package.id


def test_a_changed_artefact_creates_a_new_package_rather_than_reusing_the_authorisation(db, world):
    """NO INHERITANCE. Change an answer and the authorisation does not travel with it -
    which is the entire reason the fingerprint exists."""
    service, package = _frozen_package(db, world)
    org, agent, application = world

    changed = list(_answers())
    changed[0] = {**changed[0], "answer": "A different legal name"}
    second = service.build_package(
        application=application, answers=changed, budget=_budget(),
        contact_email="grants@warchild.org", target_url="https://funder.example.org/apply",
        mode=models.SubmissionPackage.MODE_ADAPTER,
    )
    db.commit()

    assert second.id != package.id
    assert second.package_fingerprint != package.package_fingerprint
    assert second.status == models.SubmissionPackage.AWAITING_AUTHORISATION


def test_authorising_a_package_rejects_one_that_changed_underneath(db, world):
    """Recomputed from the live row, so a client cannot authorise one package while
    another is filed."""
    service, package = _frozen_package(db, world)
    org, agent, application = world

    # Tamper with the frozen manifest without touching the stored fingerprint.
    manifest = dict(package.manifest)
    manifest["budget"] = {"currency": "UGX", "total": 999_999_999}
    package.manifest = manifest
    db.commit()

    with pytest.raises(SubmissionError) as excinfo:
        service.authorise(package_id=package.id, user_id=org.owner_user_id)
    assert "fingerprint" in str(excinfo.value).lower()


def test_an_application_that_is_not_ready_cannot_even_be_frozen(db, world):
    """Refused BEFORE a package exists, so a blocked application never leaves an
    authorisable artefact behind for someone to approve later."""
    org, agent, application = world
    application.state = "MATCHED"

    # Remove a fact readiness requires.
    db.query(models.OrgFact).filter(
        models.OrgFact.org_id == org.id, models.OrgFact.key == "country"
    ).delete()
    db.commit()

    service = _service(db, world)
    with pytest.raises(NotAuthorisable) as excinfo:
        service.build_package(
            application=application, answers=_answers(), budget=_budget(),
            target_url="https://x", mode=models.SubmissionPackage.MODE_ADAPTER,
        )
    assert excinfo.value.code == "APPLICATION_NOT_READY"


# ===========================================================================
# FILING: THE THREE OUTCOMES
# ===========================================================================
def test_a_confirmed_filing_records_a_receipt_and_marks_it_submitted(db, world):
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    run = service.execute(package_id=package.id)

    assert run.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED.value
    assert provider.submission_count == 1
    assert package.status == models.SubmissionPackage.SUBMITTED
    assert package.funder_reference

    # SUBMITTED REQUIRES A RECEIPT.
    receipt = db.query(models.SubmissionReceipt).filter(
        models.SubmissionReceipt.package_id == package.id
    ).one()
    assert receipt.reference == package.funder_reference
    assert receipt.source == models.SubmissionReceipt.SOURCE_PROVIDER


def test_the_provider_receives_exactly_the_frozen_package(db, world):
    """The payload is built from the frozen manifest, never from the live application.
    If it read through, a change after authorisation would change what the funder
    receives - the failure the fingerprint exists to prevent."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)

    assert provider.call_count == 1
    filing = provider.filings[0]
    assert filing.package_fingerprint == package.package_fingerprint
    assert filing.answer_count == len(_answers())
    assert filing.organisation_name == "War Child Test"


def test_a_lost_response_is_unknown_and_forbids_a_second_filing(db, world):
    """THE test. The funder HAS the application and Granada does not know. Retrying
    would file a second application, and many programmes disqualify both."""
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True

    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    run = service.execute(package_id=package.id)
    assert run.outcome == SubmissionOutcome.SUBMISSION_UNKNOWN.value
    assert package.status == models.SubmissionPackage.SUBMISSION_UNKNOWN
    assert provider.submission_count == 1

    # A second attempt must be REFUSED, not filed.
    second = service.execute(package_id=package.id)
    assert second.refused is True
    assert second.refusal_code == "SUBMISSION_UNKNOWN"
    assert provider.call_count == 1, "the provider was called again despite an unknown outcome"
    assert provider.submission_count == 1, "A SECOND APPLICATION WAS FILED"


def test_an_unknown_outcome_is_never_reported_as_a_failure(db, world):
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    run = service.execute(package_id=package.id)
    assert run.outcome != SubmissionOutcome.CONFIRMED_NOT_SUBMITTED.value
    assert package.status != models.SubmissionPackage.FAILED_FINAL
    assert "may have received" in (package.status_reason or "")


def test_the_unknown_path_records_an_attempt_so_reconciliation_has_evidence(db, world):
    """Append-only, because an attempt whose result was never learned is the only
    evidence reconciliation has to work from."""
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)

    attempt = db.query(models.SubmissionAttempt).filter(
        models.SubmissionAttempt.package_id == package.id
    ).one()
    assert attempt.result == SubmissionOutcome.SUBMISSION_UNKNOWN.value
    assert attempt.reconciliation_state == models.SubmissionAttempt.RECON_UNKNOWN


def test_a_definite_temporary_failure_may_be_retried(db, world):
    """The contrast with the unknown case: a failure the funder POSITIVELY confirmed is
    safe to retry, and refusing to retry it would waste a deadline."""
    provider = FakeSubmissionProvider()
    provider.fail_next = 1
    provider.fail_with = SubmissionFailure.TEMPORARY_FAILURE

    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    first = service.execute(package_id=package.id)
    assert first.outcome == SubmissionOutcome.CONFIRMED_NOT_SUBMITTED.value
    assert package.status == models.SubmissionPackage.AUTHORISED
    assert provider.submission_count == 0

    second = service.execute(package_id=package.id)
    assert second.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED.value
    assert provider.submission_count == 1


def test_a_permanent_rejection_is_final(db, world):
    provider = FakeSubmissionProvider()
    provider.fail_next = 1
    provider.fail_with = SubmissionFailure.PERMANENT_REJECTION
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    service.execute(package_id=package.id)
    assert package.status == models.SubmissionPackage.FAILED_FINAL

    again = service.execute(package_id=package.id)
    assert again.refused is True


def test_an_automation_forbidden_refusal_points_at_the_handoff(db, world):
    """A refusal on principle, not an outage. It must never be retried by a different
    route, and the handoff is the answer."""
    provider = FakeSubmissionProvider()
    provider.automation_forbidden = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    run = service.execute(package_id=package.id)
    assert run.outcome == SubmissionOutcome.CONFIRMED_NOT_SUBMITTED.value
    assert package.status == models.SubmissionPackage.FAILED_FINAL
    assert "handoff" in (package.status_reason or "").lower()
    assert provider.submission_count == 0


def test_a_passed_deadline_is_fatal_and_is_checked_before_filing(db, world):
    """A late submission is usually rejected and always noticed. Filing anyway wastes
    the organisation's credibility with the funder."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    opportunity = db.query(models.Opportunity).filter(
        models.Opportunity.id == package.opportunity_id
    ).one()
    opportunity.deadline = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "DEADLINE_PASSED"
    assert provider.call_count == 0


def test_a_document_changed_after_authorisation_blocks_the_filing(db, world):
    """The window between authorisation and filing is real, and a superseded document
    would silently change what the funder receives."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    # Supersede the frozen document: the entry survives, the row it names is gone.
    manifest = dict(package.manifest)
    manifest["documents"] = [{
        "document_id": str(uuid.uuid4()), "version": 1, "filename": "gone.pdf",
        "mime_type": "application/pdf", "checksum_sha256": "0" * 64, "storage_ref": None,
    }]
    package.manifest = manifest
    db.commit()

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert provider.call_count == 0


def test_a_paused_agent_blocks_the_filing(db, world):
    """An authorisation does not override a pause."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    agent = db.query(models.GranadaAgent).filter(models.GranadaAgent.id == package.agent_id).one()
    agent.status = "PAUSED"
    db.commit()

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "AGENT_NOT_ACTIVE"
    assert provider.call_count == 0


def test_authority_changed_after_authorisation_blocks_the_filing(db, world):
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    agent = db.query(models.GranadaAgent).filter(models.GranadaAgent.id == package.agent_id).one()
    agent.version = (agent.version or 1) + 1
    db.commit()

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "AGENT_VERSION_CHANGED"
    assert provider.call_count == 0


def test_package_creation_is_attributed_to_the_agent(db, world):
    """Never an anonymous package: the audit trail must say which agent's work this is."""
    _service_obj, package = _frozen_package(db, world)
    assert package.agent_id
    assert package.agent_version is not None


# ===========================================================================
# CAPABILITY: READING A PORTAL IS NOT FILING THROUGH IT
# ===========================================================================
def test_an_adapter_that_may_only_read_cannot_file(db, world):
    """The capability boundary. `submit_application` on a read-only adapter raises, and
    the final authority check refuses before it is ever called."""
    provider = FakeReadOnlySubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "PROVIDER_LACKS_SUBMISSION_CAPABILITY"
    assert provider.submission_count == 0


def test_the_read_only_capability_set_genuinely_excludes_submission():
    """Asserted on the value, not on prose: a set that happened to contain SUBMISSION
    would make the previous test pass for the wrong reason."""
    assert "SUBMISSION" not in READ_ONLY


def test_no_submission_provider_configured_is_refused_clearly(db, world):
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = None

    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "NO_SUBMISSION_PROVIDER"
    assert "handoff" in run.detail.lower()


# ===========================================================================
# RECEIPTS
# ===========================================================================
def test_submitted_requires_a_reference(db, world):
    """An application with no external reference is not submitted; it is POSSIBLY
    submitted, and an organisation that believes it applied when it did not has lost
    the grant and does not know."""
    _service_obj, package = _frozen_package(db, world, mode=models.SubmissionPackage.MODE_HANDOFF)
    service, package = _authorise(db, world, package)

    with pytest.raises(SubmissionError):
        service.record_receipt(package_id=package.id, reference="   ")


def test_a_person_can_record_a_receipt_from_the_portal(db, world):
    """How a HANDOFF submission completes: the person filed it, saw the confirmation,
    and records the reference here."""
    _service_obj, package = _frozen_package(
        db, world, mode=models.SubmissionPackage.MODE_HANDOFF
    )
    service, package = _authorise(db, world, package)
    org, _agent, _application = world

    service.record_receipt(
        package_id=package.id, reference="FUNDER-2027-0042",
        captured_by=org.owner_user_id,
        source=models.SubmissionReceipt.SOURCE_MANUAL,
        acknowledgement_text="Application received.",
    )
    db.commit()

    assert package.status == models.SubmissionPackage.SUBMITTED
    assert package.funder_reference == "FUNDER-2027-0042"
    receipt = db.query(models.SubmissionReceipt).filter(
        models.SubmissionReceipt.package_id == package.id
    ).one()
    assert receipt.recorded_by_agent is False
    assert receipt.source == models.SubmissionReceipt.SOURCE_MANUAL


def test_a_receipt_recorded_by_a_person_is_distinguishable_from_the_agent_s(db, world):
    """They deserve different levels of trust, so they are distinguishable in the data
    rather than only in the prose."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)

    receipt = db.query(models.SubmissionReceipt).filter(
        models.SubmissionReceipt.package_id == package.id
    ).one()
    assert receipt.recorded_by_agent is True
    assert receipt.source == models.SubmissionReceipt.SOURCE_PROVIDER


# ===========================================================================
# HANDOFF
# ===========================================================================
def test_the_handoff_files_nothing_and_produces_a_bundle(db, world):
    """The Phase 8 mode that is implemented fully, precisely because it performs no
    external action."""
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(
        db, world, mode=models.SubmissionPackage.MODE_HANDOFF
    )
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.handoff_builder = FakeHandoffBuilder()

    run = service.handoff(package_id=package.id)

    assert run.handoff is not None
    assert run.handoff.steps, "a handoff with no steps is not a handoff"
    assert run.handoff.target_url == "https://funder.example.org/apply"
    assert provider.submission_count == 0
    assert provider.call_count == 0
    # The last step is the receipt, because Granada will not claim SUBMITTED without one.
    assert "receipt" in run.handoff.steps[-1].instruction.lower()
    assert package.status != models.SubmissionPackage.SUBMITTED


def test_the_handoff_surfaces_unverified_answers(db, world):
    """An answer generated by a model and one drawn from a VERIFIED fact are different
    statements, and the organisation is accountable for both."""
    _service_obj, package = _frozen_package(
        db, world, mode=models.SubmissionPackage.MODE_HANDOFF
    )
    service, package = _authorise(db, world, package)
    service.handoff_builder = FakeHandoffBuilder()

    run = service.handoff(package_id=package.id)
    assert any("verified organisation fact" in w for w in run.handoff.warnings)


def test_the_handoff_carries_the_exact_authorised_artefacts(db, world):
    _service_obj, package = _frozen_package(
        db, world, mode=models.SubmissionPackage.MODE_HANDOFF
    )
    service, package = _authorise(db, world, package)
    service.handoff_builder = FakeHandoffBuilder()

    run = service.handoff(package_id=package.id)
    assert run.handoff.package_fingerprint == package.package_fingerprint
    assert len(run.handoff.answers) == len(_answers())


# ===========================================================================
# RECONCILIATION
# ===========================================================================
def test_reconciliation_can_confirm_an_unknown_filing_without_filing_again(db, world):
    """The resolution that makes the unknown state survivable: the funder confirms it
    has the application, so Granada records the receipt rather than filing again."""
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    service.execute(package_id=package.id)
    assert package.status == models.SubmissionPackage.SUBMISSION_UNKNOWN
    filings_before = provider.submission_count

    run = service.reconcile(package_id=package.id)

    assert run.outcome == SubmissionOutcome.CONFIRMED_SUBMITTED.value
    assert package.status == models.SubmissionPackage.SUBMITTED
    assert provider.submission_count == filings_before, "reconciliation filed a second application"
    assert package.funder_reference


def test_reconciliation_with_authoritative_absence_permits_a_retry(db, world):
    """Positive evidence that the funder does NOT have it. Only then is a retry safe."""
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)

    provider.reconcile_authoritative_absence = True
    provider.can_enumerate = True
    run = service.reconcile(package_id=package.id)

    assert run.outcome == SubmissionOutcome.CONFIRMED_NOT_SUBMITTED.value
    assert package.status == models.SubmissionPackage.AUTHORISED


def test_reconciliation_without_authoritative_absence_stays_unknown(db, world):
    """A portal that cannot enumerate its own submissions has proven nothing by not
    finding this one. Treating that as 'not received' is how a duplicate gets filed."""
    provider = FakeSubmissionProvider()
    provider.accept_then_timeout = True
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)

    provider.reconcile_authoritative_absence = True
    provider.can_enumerate = False       # cannot enumerate => absence is not evidence
    run = service.reconcile(package_id=package.id)

    assert run.outcome == SubmissionOutcome.SUBMISSION_UNKNOWN.value
    assert package.status == models.SubmissionPackage.SUBMISSION_UNKNOWN
    assert run.refused is True


def test_reconciliation_refuses_a_package_that_is_not_uncertain(db, world):
    provider = FakeSubmissionProvider()
    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider
    service.execute(package_id=package.id)   # confirmed submitted

    run = service.reconcile(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "NOT_UNCERTAIN"


def test_a_worker_crash_during_filing_leaves_the_package_recoverable(db, world):
    """A crash cannot be simulated with an Exception: the service catches `Exception`
    and would record a definite outcome. A BaseException escapes, which is the point -
    the package must stay in a state reconciliation can resolve."""
    provider = FakeSubmissionProvider()
    provider.accept_then_crash = True

    _service_obj, package = _frozen_package(db, world)
    service, package = _authorise(db, world, package)
    service.provider = provider

    from unittest.mock import patch

    with pytest.raises(BaseException):
        with patch.object(
            type(service), "_record", side_effect=KeyboardInterrupt("worker killed")
        ):
            service.execute(package_id=package.id)

    db.rollback()
    package = db.query(models.SubmissionPackage).filter(
        models.SubmissionPackage.id == package.id
    ).one()

    # The funder HAS it and Granada never recorded that. The state must not claim the
    # application definitely did not arrive.
    assert provider.submission_count == 1
    assert package.status == models.SubmissionPackage.SUBMITTING
    assert package.status in models.SubmissionPackage.UNCERTAIN

    # And a retry is refused until reconciliation resolves it.
    service.provider = provider
    run = service.execute(package_id=package.id)
    assert run.refused is True
    assert run.refusal_code == "SUBMISSION_UNKNOWN"


# ===========================================================================
# THE CEILING
# ===========================================================================
def test_autonomous_submission_remains_forbidden():
    """The capability whose absence is the guarantee that nothing files by itself."""
    for name in ("APPLICATION_SUBMISSION", "SUBMISSION"):
        capability = getattr(Capability, name, None)
        if capability is None:
            continue
        with pytest.raises(CapabilityRefused):
            assert_capability(capability)


def test_the_submission_capabilities_require_an_authorisation():
    from agent.mail.ceiling import CAPABILITIES_REQUIRING_APPROVAL

    assert Capability.SUBMISSION_HUMAN_AUTHORISED in CAPABILITIES_REQUIRING_APPROVAL
    assert Capability.SUBMISSION_HANDOFF in CAPABILITIES_REQUIRING_APPROVAL


def test_filing_without_human_approval_is_refused_by_the_ceiling():
    with pytest.raises(CapabilityRefused):
        assert_capability(Capability.SUBMISSION_HUMAN_AUTHORISED)
    # And permitted with it.
    assert_capability(Capability.SUBMISSION_HUMAN_AUTHORISED, human_approved=True)


def test_the_handoff_capability_also_requires_approval():
    with pytest.raises(CapabilityRefused):
        assert_capability(Capability.SUBMISSION_HANDOFF)
    assert_capability(Capability.SUBMISSION_HANDOFF, human_approved=True)


# ===========================================================================
# ZERO IN PRODUCTION
# ===========================================================================
def test_nothing_in_the_submission_package_can_reach_a_real_funder():
    """Asserted structurally: the package contains no HTTP client at all, so a real
    submission cannot happen by accident or by configuration."""
    import inspect

    from agent.submission import contract, service as submission_service

    for module in (contract, submission_service):
        source = inspect.getsource(module)
        for forbidden in ("requests.", "httpx.", "urllib.request", "selenium", "playwright"):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"


def test_the_only_providers_are_fakes():
    """A real adapter does not exist. There is no adapter module to misconfigure."""
    providers_dir = Path(__file__).resolve().parent.parent / "agent" / "submission" / "providers"
    names = {p.stem for p in providers_dir.glob("*.py")}
    assert names <= {"__init__", "fake"}, f"unexpected provider module(s): {names - {'__init__', 'fake'}}"
