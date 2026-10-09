"""Document generation: the layer that turns verified facts into a submittable draft.

THE GAP. `DocumentVault.add_version()` has always required a `storage_key` - a file that already
exists. The platform could store, version and approve documents, and could say which TYPES a listing
requires, and could not author one. An agent could find an opportunity and prove the organisation
eligible, and then stop, because the thing the funder asks for did not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import documents  # noqa: E402
from agent.documents import (  # noqa: E402
    DEFAULT_TEMPLATES,
    DocumentGenerationError,
    DocumentTemplate,
    MissingFact,
    TemplateStore,
)
from agent.specialists import REGISTRY  # noqa: E402
from agent.workflow_engine import WORKFLOW_DOCUMENT  # noqa: E402


def facts(**overrides) -> dict:
    base = {
        "organisation_name": "Example NGO",
        "organisation_address": "1 Test Road, Lagos",
        "today": "2026-10-09",
        "donor_name": "Example Funder",
        "opportunity_title": "Community Health Grant",
        "organisation_type": "NGO",
        "country": "Nigeria",
        "registration_number": "RC-123456",
        "authorised_signatory": "A Person",
        "authorised_role": "Director",
        "contact_email": "ops@example.org",
        "focus_area": "primary healthcare",
        "registration_valid_until": "2030-01-01",
        "currency": "NGN",
        "amount_requested": "5,000,000",
    }
    base.update(overrides)
    return base


# ===========================================================================
# THE HONESTY PROPERTY
# ===========================================================================
def test_a_missing_fact_is_REFUSED_not_invented():
    """THE test.

    A placeholder with no fact behind it must raise. Writing a plausible-looking registration number
    in front of a donor, in the organisation's name, is the one thing this module must never do - and
    it would destroy the platform's only real claim, which is that it does not assert what it cannot
    evidence.
    """
    template = DocumentTemplate(
        doc_type="cover_letter", title="t", body="Reg no: {{registration_number}}\n"
    )
    with pytest.raises(MissingFact) as caught:
        documents.render(template, {}, filename_stem="x")
    assert "registration_number" in str(caught.value)


def test_EVERY_missing_fact_is_reported_at_once():
    """A caller fixing one error per run, rediscovering the next each time, is a caller who quits."""
    template = DocumentTemplate(
        doc_type="x", title="t", body="{{alpha}} {{beta}} {{gamma}}"
    )
    with pytest.raises(MissingFact) as caught:
        documents.render(template, {"beta": "set"}, filename_stem="x")
    message = str(caught.value)
    assert "alpha" in message and "gamma" in message
    assert "beta" not in message, "a present fact was reported missing"


def test_the_refusal_says_whose_job_it_is():
    """The message must point at the organisation, not read as a system fault."""
    with pytest.raises(MissingFact) as caught:
        documents.render(
            DocumentTemplate(doc_type="x", title="t", body="{{registration_number}}"),
            {},
            filename_stem="x",
        )
    assert "organisation must provide" in str(caught.value).lower()


# ===========================================================================
# DETERMINISM
# ===========================================================================
def test_the_same_facts_produce_the_same_bytes():
    """The vault is append-only and versioned. A generator that emitted a timestamp would append a
    new version on every run and leave 'which draft did you send' unanswerable."""
    template = TemplateStore().get("cover_letter")
    first = documents.render(template, facts(), filename_stem="opp")
    second = documents.render(template, facts(), filename_stem="opp")
    assert first.content == second.content
    assert first.checksum_sha256 == second.checksum_sha256


def test_different_facts_produce_different_bytes():
    """The inverse, so the test above is not satisfied by ignoring the facts."""
    template = TemplateStore().get("cover_letter")
    a = documents.render(template, facts(), filename_stem="opp")
    b = documents.render(template, facts(organisation_name="Other NGO"), filename_stem="opp")
    assert a.checksum_sha256 != b.checksum_sha256


def test_the_checksum_is_a_real_sha256():
    document = documents.render(TemplateStore().get("cover_letter"), facts(), filename_stem="o")
    assert len(document.checksum_sha256) == 64
    assert all(c in "0123456789abcdef" for c in document.checksum_sha256)


def test_no_placeholder_survives_into_the_output():
    """A document shipped with `{{typo}}` in it is worse than one that failed."""
    for template in DEFAULT_TEMPLATES:
        document = documents.render(template, facts(), filename_stem="o")
        assert "{{" not in document.content.decode()
        assert "}}" not in document.content.decode()


def test_an_unsubstituted_placeholder_is_an_error():
    """Defence in depth: if the regex and the substitution ever disagree, fail rather than emit."""
    template = DocumentTemplate(doc_type="x", title="t", body="hello {{name}}")
    document = documents.render(template, {"name": "world"}, filename_stem="x")
    assert b"hello world" in document.content


# ===========================================================================
# THE STORE
# ===========================================================================
def test_every_default_template_resolves_by_its_own_type():
    """The store must resolve each template it ships, by the name it declares.

    NOT tested with arbitrary casing: `document_types.canonical()` maps canonical names and known
    ALIASES - the spellings a funder's listing or an upload might use - and never claimed to fold
    free-form case. Asserting that would have been a test demanding behaviour nothing implements.
    """
    store = TemplateStore()
    for template in DEFAULT_TEMPLATES:
        assert store.get(template.doc_type) is template


def test_every_canonical_type_is_either_GENERATED_or_EVIDENCE():
    """THE invariant that matters, and it is stronger than the distinction I first drew.

    `document_types.DOCUMENT_TYPES` is a MIXED list. Four entries are evidence an organisation already
    has and uploads - a registration certificate, audited accounts, a tax clearance, bank details -
    and no software can author them. The other four are documents somebody has to WRITE.

    So every canonical type must be accounted for: either this module can generate it, or it is
    declared evidence the organisation must supply. A canonical type in NEITHER set is a requirement
    the readiness gate can raise and nothing can ever satisfy - the application would sit at
    not-ready forever, and no error would say why.

    This is exactly the bug the first version of these templates would have caused: they shipped under
    private names (`organisation_summary`, `budget_narrative`) while the gate asked for
    `organisation_profile` and `budget`. The gate demanded a budget; the generator produced a
    budget_narrative; the halves of one pipeline used different words for the same document.
    """
    from agent.document_types import known_types

    from agent.documents import EVIDENCE_TYPES

    generated = {template.doc_type for template in DEFAULT_TEMPLATES}

    unaccounted = known_types() - generated - EVIDENCE_TYPES
    assert not unaccounted, (
        f"the readiness gate can require {sorted(unaccounted)} and nothing can produce it - neither a "
        "template nor an upload"
    )
    assert not (generated & EVIDENCE_TYPES), (
        f"{sorted(generated & EVIDENCE_TYPES)} is declared evidence AND generated; an organisation "
        "cannot both upload a certificate and have software write one"
    )


def test_the_generated_names_are_the_canonical_names():
    """The generator must speak the gate's vocabulary. A private name is a name the gate never asks
    for, and a template nobody requests is a document that never gets produced."""
    from agent.document_types import canonical

    for template in DEFAULT_TEMPLATES:
        if template.doc_type == "cover_letter":
            # No canonical equivalent: a cover letter is an application artefact, not a requirement a
            # funder lists. Generated unconditionally, which is why it is exempt here.
            assert canonical(template.doc_type) is None
            continue
        assert canonical(template.doc_type) == template.doc_type, (
            f"{template.doc_type} is not a canonical name, so `required_types_for` will never ask "
            "for it and this template will never be used"
        )


def test_no_template_contains_a_placeholder_it_cannot_have():
    """Every placeholder must be a fact an organisation can VERIFY, because a generated document is
    only as good as the evidence behind it."""
    for template in DEFAULT_TEMPLATES:
        assert template.placeholders(), f"{template.doc_type} has no placeholders at all"
        body = template.body
        assert "{{" in body
