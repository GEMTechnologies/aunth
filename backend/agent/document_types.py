"""One canonical list of document types, so the gate and the vault cannot drift.

THE DEFECT THIS FIXES
---------------------
`workspace.readiness()` required ``doc_type == "audited_accounts"`` for any opportunity
whose text mentions "audited accounts" or "audited financial". **Nothing in the codebase
ever produced that type.** Every fixture and helper - `tests/test_mail.py`,
`tests/test_agent_api.py`, `tests/test_mail_outbound.py` - creates
``doc_type="audited_financial_statements"``.

So an organisation that uploaded its audited accounts under the name the product uses would
be told **"a audited accounts is required but not held"**, forever, and could never submit
to any funder whose listing mentions audited accounts. There was no canonical list anywhere
- no constant, no enum - so nothing could catch the drift.

WHY IT SURVIVED
---------------
Every existing test opportunity's eligibility text omits those phrases, so the branch never
ran. It took an end-to-end journey test, walking a realistic listing, to reach it. That is
precisely what the journey test is for, and the reason "the unit tests pass" is not the same
claim as "the flow works".

THE SHAPE OF THE FIX
--------------------
One registry, and the gate asks it. Synonyms are explicit rather than fuzzy: a document type
is a controlled vocabulary, and matching on substrings would make ``audited_accounts`` and
``unaudited_accounts`` the same thing.
"""

from __future__ import annotations

#: Canonical document types, with the alternative names a real organisation would use.
#:
#: The FIRST name is canonical. The rest are accepted because an organisation uploading
#: "Audited Financial Statements" and one uploading "Audited Accounts" have done the same
#: thing, and a gate that disagreed would block a submission over a filename.
DOCUMENT_TYPES: dict[str, tuple[str, ...]] = {
    "registration_certificate": (
        "registration_certificate",
        "certificate_of_registration",
        "certificate_of_incorporation",
        "registration_cert",
    ),
    # THE PAIR THAT WAS BROKEN. `audited_accounts` is what the readiness gate asks for;
    # `audited_financial_statements` is what the product's own helpers create.
    "audited_accounts": (
        "audited_accounts",
        "audited_financial_statements",
        "audited_financials",
        "audited_statements",
        "financial_statements",
    ),
    "tax_clearance": (
        "tax_clearance",
        "tax_clearance_certificate",
        "tax_compliance",
        "tax_compliance_certificate",
    ),
    "bank_details": (
        "bank_details",
        "bank_account_details",
        "bank_confirmation",
        "bank_letter",
    ),
    "safeguarding_policy": (
        "safeguarding_policy",
        "child_protection_policy",
        "protection_policy",
    ),
    "budget": ("budget", "project_budget", "budget_narrative"),
    "workplan": ("workplan", "implementation_plan", "logframe", "results_framework"),
    "organisation_profile": ("organisation_profile", "org_profile", "capability_statement"),
}

#: Requirement phrases found in an opportunity's text -> the canonical type they imply.
#:
#: Used by the readiness gate. The phrases are matched case-insensitively against the
#: listing's own words, which is the only signal available before an organisation has
#: configured anything.
REQUIREMENT_PHRASES: dict[str, tuple[str, ...]] = {
    "registration_certificate": (
        "registration certificate",
        "certificate of registration",
        "certificate of incorporation",
    ),
    "audited_accounts": (
        "audited accounts",
        "audited financial",
        "audited statements",
        "audit report",
    ),
    "tax_clearance": ("tax clearance", "tax compliance"),
    "bank_details": ("bank details", "bank account", "bank confirmation"),
    "safeguarding_policy": ("safeguarding policy", "child protection policy"),
}


def canonical(doc_type: str | None) -> str | None:
    """The canonical name for a document type, or None when it is unrecognised.

    Returning None rather than the input is deliberate: an unrecognised type is a type
    nobody declared, and silently accepting it would let the vocabulary drift again.
    """
    if not doc_type:
        return None
    wanted = doc_type.strip().casefold()
    for name, alternatives in DOCUMENT_TYPES.items():
        if wanted == name or wanted in {a.casefold() for a in alternatives}:
            return name
    return None


def is_same_type(left: str | None, right: str | None) -> bool:
    """Whether two document type names mean the same thing.

    Exact after canonicalisation. Not substring matching: ``audited_accounts`` and
    ``unaudited_accounts`` are different documents, and a gate that confused them would
    accept an unaudited statement where an audited one was required.
    """
    left_canonical, right_canonical = canonical(left), canonical(right)
    if left_canonical is None or right_canonical is None:
        # An unrecognised type only equals itself by exact match, so a custom type a
        # deployment invented still works for its own requirement.
        return bool(left) and bool(right) and left.strip().casefold() == right.strip().casefold()
    return left_canonical == right_canonical


def required_types_for(text: str | None) -> list[str]:
    """Which canonical document types a listing's text asks for.

    Ordered and de-duplicated, so the gate reports blockers in a stable order - a readiness
    report whose contents reorder between calls is one nobody can diff.
    """
    if not text:
        return []
    lowered = text.casefold()
    found: list[str] = []
    for doc_type, needles in REQUIREMENT_PHRASES.items():
        if any(needle in lowered for needle in needles):
            found.append(doc_type)
    return found


def known_types() -> frozenset[str]:
    """Every canonical type, for validation and for a test that asserts the gate's
    requirements are all producible."""
    return frozenset(DOCUMENT_TYPES)
