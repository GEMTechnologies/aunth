"""Granada Mail — the vocabulary shared by the whole mail subsystem.

One module holds the enums because they are used by the gateway, the classifier,
the correlation engine, the security screen and the schema, and scattering them
would let two modules disagree about what ``AMBIGUOUS`` means.

**The capability ceiling lives here**, and it is a single list rather than a
convention. `PHASE_7A_ALLOWED` and `PHASE_7A_FORBIDDEN` are the brief's two lists
verbatim, and `assert_capability` is the only sanctioned way to ask whether an
action is permitted. An action is refused unless it is explicitly allowed, so a
capability added later cannot become available by being omitted.
"""

from __future__ import annotations

from enum import Enum


# The capability ceiling, outbound risk classes and the approval requirement all
# live in `ceiling`, re-exported here because this module is where the rest of the
# mail subsystem already imports from.
#
# Defined in ONE place deliberately. Two capability enums would let the inbound
# ceiling and the outbound ceiling drift, and the failure mode is precisely the one
# the brief warns about: "the mailbox can be read" quietly becoming "the mailbox may
# send" because a list was widened in one file and not the other.
from agent.mail.ceiling import (  # noqa: E402
    CAPABILITIES_REQUIRING_APPROVAL,
    HIGH_RISK_CLASSES,
    PHASE_7A_ALLOWED,
    PHASE_7A_FORBIDDEN,
    PHASE_7B_ALLOWED,
    PHASE_7B_FORBIDDEN,
    SENDABLE_RISK_CLASSES,
    ApprovalRequired,
    Capability,
    CapabilityRefused,
    ExternalActionDisabled,
    OutboundRisk,
    RiskRefused,
    assert_capability,
)


class MailClassification(str, Enum):
    """What an inbound message appears to be.

    ``UNKNOWN`` is a real answer rather than a fallback to be avoided: a message
    Granada does not understand is one a person should read, and labelling it
    ``GENERAL_QUESTION`` to avoid an empty field would hide that.
    """

    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    DOCUMENT_REQUEST = "DOCUMENT_REQUEST"
    DEADLINE_CHANGE = "DEADLINE_CHANGE"
    INTERVIEW_INVITATION = "INTERVIEW_INVITATION"
    SHORTLIST_NOTICE = "SHORTLIST_NOTICE"
    AWARD_NOTICE = "AWARD_NOTICE"
    REJECTION_NOTICE = "REJECTION_NOTICE"
    CONTRACT = "CONTRACT"
    FINANCIAL_REQUEST = "FINANCIAL_REQUEST"
    BANK_DETAIL_REQUEST = "BANK_DETAIL_REQUEST"
    GENERAL_QUESTION = "GENERAL_QUESTION"
    APPLICATION_STATUS = "APPLICATION_STATUS"
    AUTOMATED_NOTIFICATION = "AUTOMATED_NOTIFICATION"
    BOUNCE = "BOUNCE"
    SPAM_OR_SUSPICIOUS = "SPAM_OR_SUSPICIOUS"
    UNKNOWN = "UNKNOWN"


#: Classifications that must never be acted on without a person, whatever the
#: confidence. These are the ones where being wrong moves money, accepts a
#: contract, or hands over banking details.
SENSITIVE_CLASSIFICATIONS: frozenset[str] = frozenset({
    MailClassification.CONTRACT.value,
    MailClassification.FINANCIAL_REQUEST.value,
    MailClassification.BANK_DETAIL_REQUEST.value,
    MailClassification.AWARD_NOTICE.value,
})


class CorrelationState(str, Enum):
    """How confidently a message was attached to an application.

    ``AUTONOMOUS_STATES`` below is the whole safety property: only these two may
    trigger application-specific work without a human. The brief's rule governs -
    wrong linkage is worse than no linkage - because a message linked to the wrong
    application produces a draft about the wrong grant, quoting the wrong deadline,
    to the wrong funder.
    """

    EXACT = "EXACT"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    AMBIGUOUS = "AMBIGUOUS"
    UNLINKED = "UNLINKED"

    @property
    def may_act_autonomously(self) -> bool:
        return self in (CorrelationState.EXACT, CorrelationState.HIGH_CONFIDENCE)


class LinkMethod(str, Enum):
    """Which signal produced the link. Recorded so a link is explainable."""

    REPLY_ALIAS_TOKEN = "REPLY_ALIAS_TOKEN"
    THREAD_PROVIDER_ID = "THREAD_PROVIDER_ID"
    IN_REPLY_TO_CHAIN = "IN_REPLY_TO_CHAIN"
    INTERNET_MESSAGE_ID = "INTERNET_MESSAGE_ID"
    APPLICATION_REFERENCE = "APPLICATION_REFERENCE"
    CORRELATION_HEADER = "CORRELATION_HEADER"
    KNOWN_DONOR_AND_SUBJECT = "KNOWN_DONOR_AND_SUBJECT"
    UNIQUE_OPEN_APPLICATION = "UNIQUE_OPEN_APPLICATION"
    NONE = "NONE"


class SecurityFlag(str, Enum):
    """What the deterministic screen noticed. Signals, never verdicts."""

    PROMPT_INJECTION_ATTEMPT = "PROMPT_INJECTION_ATTEMPT"
    INSTRUCTION_OVERRIDE_ATTEMPT = "INSTRUCTION_OVERRIDE_ATTEMPT"
    AUTHORITY_IMPERSONATION = "AUTHORITY_IMPERSONATION"
    CREDENTIAL_REQUEST = "CREDENTIAL_REQUEST"
    BANK_DETAIL_REQUEST = "BANK_DETAIL_REQUEST"
    DISPLAY_NAME_MISMATCH = "DISPLAY_NAME_MISMATCH"
    SENDER_DOMAIN_UNVERIFIED = "SENDER_DOMAIN_UNVERIFIED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHENTICATION_ABSENT = "AUTHENTICATION_ABSENT"
    SUSPICIOUS_URL = "SUSPICIOUS_URL"
    TRACKING_RESOURCE = "TRACKING_RESOURCE"
    HTML_SCRIPT = "HTML_SCRIPT"
    REMOTE_IMAGE = "REMOTE_IMAGE"
    DANGEROUS_ATTACHMENT = "DANGEROUS_ATTACHMENT"
    EXECUTABLE_ATTACHMENT = "EXECUTABLE_ATTACHMENT"
    OVERSIZE_ATTACHMENT = "OVERSIZE_ATTACHMENT"
    REPLY_TO_MISMATCH = "REPLY_TO_MISMATCH"
    LOOKALIKE_DOMAIN = "LOOKALIKE_DOMAIN"


class DraftStatus(str, Enum):
    GENERATING = "GENERATING"
    READY = "READY"
    NEEDS_DATA = "NEEDS_DATA"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    #: Unreachable in Phase 7a. Its presence is a description of the future, not a
    #: code path: nothing writes it and the provider refuses the capability.
    SENT = "SENT"


class DocumentRequestType(str, Enum):
    """Document kinds Granada can be asked for, mapped to vault document types.

    A closed vocabulary on purpose. Free-text document names would let a request
    for "audited accounts" match a vault document called "accounts" that is not
    audited, and attaching the wrong document to a funder is worse than replying
    that we do not have it.
    """

    AUDITED_FINANCIAL_STATEMENTS = "AUDITED_FINANCIAL_STATEMENTS"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    REGISTRATION_CERTIFICATE = "REGISTRATION_CERTIFICATE"
    TAX_CLEARANCE = "TAX_CLEARANCE"
    BANK_CONFIRMATION = "BANK_CONFIRMATION"
    ORGANISATION_PROFILE = "ORGANISATION_PROFILE"
    PROJECT_PROPOSAL = "PROJECT_PROPOSAL"
    BUDGET = "BUDGET"
    LOGFRAME = "LOGFRAME"
    SAFEGUARDING_POLICY = "SAFEGUARDING_POLICY"
    REFERENCE_LETTER = "REFERENCE_LETTER"
    INSURANCE_CERTIFICATE = "INSURANCE_CERTIFICATE"
    UNKNOWN = "UNKNOWN"


#: Which vault document type satisfies a request. ``None`` means Granada will not
#: substitute anything, and the request becomes a data requirement.
DOCUMENT_TYPE_MAP: dict[DocumentRequestType, tuple[str, ...]] = {
    DocumentRequestType.AUDITED_FINANCIAL_STATEMENTS: (
        "audited_financial_statements", "financial_statements",
    ),
    DocumentRequestType.ANNUAL_REPORT: ("annual_report",),
    DocumentRequestType.REGISTRATION_CERTIFICATE: ("registration_certificate",),
    DocumentRequestType.TAX_CLEARANCE: ("tax_clearance", "tax_compliance_certificate"),
    DocumentRequestType.BANK_CONFIRMATION: ("bank_confirmation", "bank_letter"),
    DocumentRequestType.ORGANISATION_PROFILE: ("organisation_profile",),
    DocumentRequestType.SAFEGUARDING_POLICY: ("safeguarding_policy", "child_protection_policy"),
    DocumentRequestType.INSURANCE_CERTIFICATE: ("insurance_certificate",),
    # Deliberately unmapped. A proposal or budget is application-specific work
    # rather than a standing organisation document, so Granada must not guess.
    DocumentRequestType.PROJECT_PROPOSAL: (),
    DocumentRequestType.BUDGET: (),
    DocumentRequestType.LOGFRAME: (),
    DocumentRequestType.REFERENCE_LETTER: (),
    DocumentRequestType.UNKNOWN: (),
}
