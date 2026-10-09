"""Document generation: turning an organisation's facts into a submittable draft.

THE GAP THIS FILLS
------------------
`DocumentVault.add_version()` has always demanded a `storage_key` - a file that ALREADY exists. The
platform could store, version and approve documents and could say which TYPES a listing requires
(`document_types.required_types_for`), and it could not author one. So an agent could find an
opportunity and prove the organisation was eligible, and then stop, because the thing the funder
actually asks for did not exist.

Four decisions, and each one is load-bearing:

**1. REFUSE, NEVER INVENT.** A template placeholder with no fact behind it raises. The alternative -
writing `[REGISTRATION NUMBER]` or, far worse, a plausible-looking number - puts a fabrication in
front of a donor in the organisation's name. This matches the eligibility engine, which returns
`NEEDS_DATA` rather than guessing country eligibility. The platform's whole claim is that it does not
assert what it cannot evidence; a document generator that filled gaps would quietly destroy that.

**2. DETERMINISTIC.** The same facts and the same opportunity produce byte-identical output, so the
checksum is stable. That matters because the vault is append-only and versioned: a generator that
emitted a timestamp or a random id would append a new version on every run, and a funder asking
"which draft did you send" would have no answer.

**3. GENERATED IS NOT APPROVED.** Documents are written `PENDING` and the vault enforces that. An
agent must not be able to approve its own output into a submission - a draft is a proposal to a human,
not a fact.

**4. THESE ARE APPLICATION DOCUMENTS, NOT EVIDENCE DOCUMENTS.** `document_types.DOCUMENT_TYPES` is
the canonical list of evidence - a registration certificate, audited accounts, a tax clearance. An
organisation *already has* those and uploads them; the platform validates and requires them, and no
software can generate them. What this module writes is the other category: the cover letter, the
organisation summary, the budget narrative. Conflating the two would have the generator promising a
registration certificate.

**5. NO TEMPLATE ENGINE.** Substitution is a strict `{{key}}` lookup over a known mapping, not Jinja2.
The facts being interpolated originate outside this system - a funder's listing text, an uploaded
certificate - and a template engine that evaluates expressions over such input is an injection surface
for no benefit. An unknown placeholder is an error, not a silently-rendered empty string.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: `{{key}}` and nothing else. Deliberately not a general expression syntax.
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", re.IGNORECASE)


class DocumentGenerationError(RuntimeError):
    """A document could not be produced, with the reason a human needs to fix it."""


class MissingFact(DocumentGenerationError):
    """A placeholder had no fact behind it.

    Raised rather than rendered: an invented registration number in front of a donor is worse than no
    document at all.
    """


@dataclass(frozen=True)
class DocumentTemplate:
    """One document type's body.

    `version` is recorded on the generated document, because "which template produced this" is the
    question asked when a donor queries a figure six months later.
    """

    doc_type: str
    title: str
    body: str
    version: str = "1"
    mime_type: str = "text/markdown"

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.body))


@dataclass
class GeneratedDocument:
    """A rendered document, ready to be written to the vault by the caller."""

    doc_type: str
    title: str
    content: bytes
    mime_type: str
    checksum_sha256: str
    template_version: str
    filename: str
    #: Which facts were used, so the document is traceable to its inputs.
    facts_used: dict[str, str] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ===========================================================================
# The templates
# ===========================================================================
#: Deliberately plain and short. A real deployment replaces these per donor; they exist so the
#: pipeline has something honest to produce and so the tests measure the mechanism rather than the
#: prose. Every placeholder here must be a fact the organisation can actually VERIFY, because a
#: generated document is only as good as the evidence behind it.
#: Canonical evidence types the organisation must UPLOAD. No software can generate a registration
#: certificate, so the generator skips these rather than pretending to satisfy them - and says so,
#: because "we have no template" and "you must upload this" are different answers to a readiness check.
EVIDENCE_TYPES: frozenset[str] = frozenset(
    {"registration_certificate", "audited_accounts", "tax_clearance", "bank_details"}
)

#: The templates. Names match `document_types.DOCUMENT_TYPES` wherever a canonical name exists, so the
#: readiness gate and the generator cannot disagree about which document is which. `cover_letter` has
#: no canonical equivalent - it is an application artefact, not something a funder names as a
#: requirement - so it is generated unconditionally.
DEFAULT_TEMPLATES: tuple[DocumentTemplate, ...] = (
    DocumentTemplate(
        doc_type="cover_letter",
        title="Cover Letter",
        body=(
            "# Cover Letter\n\n"
            "{{organisation_name}}\n"
            "{{organisation_address}}\n\n"
            "{{today}}\n\n"
            "Dear {{donor_name}},\n\n"
            "We submit our application for **{{opportunity_title}}**.\n\n"
            "{{organisation_name}} is a registered {{organisation_type}} operating in "
            "{{country}}, registration number {{registration_number}}.\n\n"
            "We confirm the information in this application is accurate and that the organisation "
            "is in good standing.\n\n"
            "Yours faithfully,\n"
            "{{authorised_signatory}}, {{authorised_role}}\n"
        ),
        version="1",
    ),
    DocumentTemplate(
        doc_type="organisation_profile",
        title="Organisation Profile",
        body=(
            "# Organisation Profile\n\n"
            "**Legal name:** {{organisation_name}}\n"
            "**Registration number:** {{registration_number}}\n"
            "**Registration valid until:** {{registration_valid_until}}\n"
            "**Country of operation:** {{country}}\n"
            "**Organisation type:** {{organisation_type}}\n"
            "**Contact:** {{contact_email}}\n\n"
            "## Purpose\n\n"
            "{{organisation_name}} works in {{country}} on {{focus_area}}.\n"
        ),
        version="1",
    ),
    DocumentTemplate(
        doc_type="budget",
        title="Budget",
        body=(
            "# Budget\n\n"
            "Application for **{{opportunity_title}}** ({{donor_name}}).\n\n"
            "Requested amount: **{{currency}} {{amount_requested}}**\n\n"
            "All figures are stated in {{currency}} and are supported by the organisation's audited "
            "accounts.\n"
        ),
        version="1",
    ),
    DocumentTemplate(
        doc_type="workplan",
        title="Workplan",
        body=(
            "# Workplan\n\n"
            "**Activity:** {{opportunity_title}}\n"
            "**Implementing organisation:** {{organisation_name}}\n"
            "**Location:** {{country}}\n\n"
            "This workplan sets out the activities {{organisation_name}} will deliver, and is "
            "submitted together with the budget and organisation profile.\n"
        ),
        version="1",
    ),
    DocumentTemplate(
        doc_type="safeguarding_policy",
        title="Safeguarding Policy",
        body=(
            "# Safeguarding Policy\n\n"
            "**Organisation:** {{organisation_name}}\n"
            "**Registration number:** {{registration_number}}\n"
            "**Effective from:** {{today}}\n\n"
            "{{organisation_name}} is committed to the protection of the people it works with, and "
            "operates a safeguarding policy covering staff, volunteers and partners in {{country}}.\n"
        ),
        version="1",
    ),
)


class TemplateStore:
    """Resolves a document type to a template.

    Returns `None` for a type with no template rather than raising, because "we have no template for
    this" and "this template is broken" are different answers and the caller reports them
    differently.
    """

    def __init__(self, templates: Optional[tuple[DocumentTemplate, ...]] = None) -> None:
        self._templates = {t.doc_type: t for t in (templates or DEFAULT_TEMPLATES)}

    def get(self, doc_type: str) -> Optional[DocumentTemplate]:
        """Exact name first, canonical alias second.

        The order matters. `document_types.canonical()` maps the EVIDENCE types - a registration
        certificate, audited accounts - and returns None for a generated document like a cover letter,
        which is not one of them. Trying canonicalisation first made the store unable to resolve the
        templates it ships.
        """
        if doc_type in self._templates:
            return self._templates[doc_type]

        from agent.document_types import canonical

        key = canonical(doc_type)
        return self._templates.get(key) if key else None

    def available(self) -> list[str]:
        return sorted(self._templates)


def render(
    template: DocumentTemplate,
    facts: dict[str, Any],
    *,
    filename_stem: str,
) -> GeneratedDocument:
    """Fill a template from `facts`, or raise naming exactly what is missing.

    EVERY missing placeholder is reported at once rather than the first one. A caller fixing a
    document one error at a time, re-running, and discovering the next is a caller who gives up.
    """
    missing = sorted(
        name for name in template.placeholders() if not str(facts.get(name, "") or "").strip()
    )
    if missing:
        raise MissingFact(
            f"{template.doc_type}: cannot generate without {', '.join(missing)}. "
            "These are facts the organisation must provide and verify; inventing them would put a "
            "fabrication in front of a donor in the organisation's name."
        )

    used: dict[str, str] = {}
    body = template.body
    for name in sorted(template.placeholders()):
        value = str(facts[name]).strip()
        used[name] = value
        body = _PLACEHOLDER.sub(
            lambda m, _n=name, _v=value: _v if m.group(1).lower() == _n else m.group(0), body
        )

    # A placeholder that survived substitution means the regex and the lookup disagreed, which would
    # ship a document with `{{typo}}` in it. Fail loudly rather than emit it.
    leftover = _PLACEHOLDER.findall(body)
    if leftover:
        raise DocumentGenerationError(
            f"{template.doc_type}: unsubstituted placeholders remain: {sorted(set(leftover))}"
        )

    content = body.encode("utf-8")
    return GeneratedDocument(
        doc_type=template.doc_type,
        title=template.title,
        content=content,
        mime_type=template.mime_type,
        checksum_sha256=checksum(content),
        template_version=template.version,
        filename=f"{filename_stem}-{template.doc_type}.md",
        facts_used=used,
    )


def required_facts(template: DocumentTemplate) -> list[str]:
    """What a caller must supply before this template can render. For a readiness report."""
    return sorted(template.placeholders())


def facts_from_organisation(memory: Any) -> dict[str, Any]:
    """The fact names a document draws on, read from organisation memory.

    Only SUBMISSION-SAFE facts are returned by `submission_facts()`, which is the point: a document is
    a submission artefact, and an unverified value must not reach a funder through one.
    """
    return dict(memory.submission_facts())


def today() -> str:
    """The date a document is dated. Injected rather than read inside `render`, so a test can pin it
    and so two runs on the same day are byte-identical."""
    return datetime.now(timezone.utc).date().isoformat()
