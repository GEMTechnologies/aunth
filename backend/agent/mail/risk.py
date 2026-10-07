"""Outbound risk classification, and the safety checks on recipients and attachments.

Risk classification
-------------------
The question the classifier answers is narrow and consequential:

    could sending this move money, accept an obligation, or hand over a credential?

Because the answer decides whether an ordinary approval can authorise it at all. A
message that changes banking details is **not sendable through the Phase 7b
approval path, even with a human approval** - a human can be rushed, mistaken, or
acting on a forged instruction, and the platform must not let one click commit the
organisation.

The classifier reads the FROZEN intent, not the draft, so the class a human saw is
the class that was approved.

Recipient safety
----------------
The brief's rule: a recipient set must come from durable records or explicit human
entry, and **never from an LLM alone**. So this module does not accept "the model
suggested grants@funder.example". It accepts addresses that are already attached to
the thread, the application, or the identity, or that a person typed - and it
reports the risks it sees rather than silently allowing them.

Almost everything here is *reported*, not blocked. An unexpected domain is a
warning, not a refusal, because a programme officer legitimately writing to a new
colleague should not be stopped. The exceptions are the things that are never
legitimate: another organisation's address book, or a recipient that is not an
address at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from agent.mail.ceiling import HIGH_RISK_CLASSES, OutboundRisk
from agent.mail.security import domain_of, _FREEMAIL_DOMAINS, _SHORTENER_DOMAINS, _looks_like_lookalike

#: A pragmatic address check. Deliberately not a full RFC 5322 parser: the goal is
#: to refuse obvious non-addresses before a person approves something that cannot be
#: sent, not to accept every exotic-but-legal form.
_ADDRESS = re.compile(r"^[^@\s,;]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

#: Above this, a warning: a donor reply with thirty recipients is more likely a
#: mistake or a mailing list than an intended reply.
LARGE_RECIPIENT_SET = 10


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------
#: Ordered by consequence, most severe first, so the first match wins.
_RISK_PATTERNS: tuple[tuple[OutboundRisk, re.Pattern[str], str], ...] = (
    (
        OutboundRisk.CREDENTIAL_SECURITY,
        re.compile(
            r"\b(password|passphrase|api\s*key|secret\s+key|private\s+key|"
            r"login\s+credential|access\s+token|2fa|one[- ]time\s+code|otp)\b",
            re.I,
        ),
        "the message discusses credentials or access secrets",
    ),
    (
        OutboundRisk.BANKING,
        re.compile(
            r"\b(bank\s+(account|details?|coordinates)|account\s+number|iban|"
            r"swift\s+code|\bbic\b|routing\s+number|sort\s+code|"
            r"wire\s+(transfer|the\s+funds)|remit\s+to|payment\s+details?|"
            r"change\s+(our|the|your)?\s*(bank|account))\b",
            re.I,
        ),
        "the message discusses banking details",
    ),
    (
        OutboundRisk.CONTRACT_RELATED,
        re.compile(
            r"\b(grant\s+agreement|contract|memorandum\s+of\s+understanding|\bmou\b|"
            r"we\s+accept\s+the\s+(award|grant|terms)|accept(ing)?\s+the\s+terms|"
            r"sign(ed|ature)?\s+(and\s+return|the\s+agreement)|countersign|"
            r"binding\s+agreement|terms\s+and\s+conditions\s+are\s+accepted)\b",
            re.I,
        ),
        "the message accepts or discusses a contract",
    ),
    (
        OutboundRisk.LEGAL,
        re.compile(
            r"\b(legal(ly)?\s+(binding|obligat)|indemnif|liabilit(y|ies)|"
            r"warrant(y|ies)|governing\s+law|jurisdiction|arbitration|"
            r"we\s+certify|i\s+certify|under\s+penalty\s+of\s+perjury|"
            r"data\s+protection\s+undertaking)\b",
            re.I,
        ),
        "the message creates or discusses a legal obligation",
    ),
    (
        OutboundRisk.FINANCIAL,
        re.compile(
            r"\b(invoice|disbursement|tranche|financial\s+commitment|"
            r"we\s+commit\s+to\s+(fund|spend)|budget\s+revision|"
            r"authoris(e|ing)\s+payment|authoriz(e|ing)\s+payment|"
            r"purchase\s+order|procurement\s+commitment)\b",
            re.I,
        ),
        "the message concerns money",
    ),
    (
        OutboundRisk.AWARD_RELATED,
        re.compile(
            r"\b(award|grant\s+(offer|approval)|we\s+are\s+pleased\s+to\s+(inform|confirm)|"
            r"successful\s+(application|proposal))\b",
            re.I,
        ),
        "the message concerns an award",
    ),
    (
        OutboundRisk.INTERVIEW_RESPONSE,
        re.compile(r"\b(interview|panel\s+(discussion|meeting)|due\s+diligence\s+(call|meeting))\b", re.I),
        "the message responds about an interview",
    ),
    (
        OutboundRisk.DEADLINE_RESPONSE,
        re.compile(r"\b(deadline|extension|due\s+date|closing\s+date|time\s+extension)\b", re.I),
        "the message concerns a deadline",
    ),
    (
        OutboundRisk.DOCUMENT_RESPONSE,
        re.compile(
            r"\b(attached|attachment|enclosed|please\s+find|find\s+attached|"
            r"financial\s+statement|annual\s+report|registration\s+certificate|"
            r"audited\s+accounts|supporting\s+document)\b",
            re.I,
        ),
        "the message sends documents",
    ),
    (
        OutboundRisk.APPLICATION_INFORMATION,
        re.compile(
            r"\b(our\s+application|application\s+(reference|number)|proposal|"
            r"organisation\s+profile|we\s+are\s+(applying|submitting))\b",
            re.I,
        ),
        "the message concerns the application",
    ),
)


@dataclass
class RiskAssessment:
    """The class, and the evidence for it."""

    risk_class: OutboundRisk
    reasons: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)

    @property
    def is_high_risk(self) -> bool:
        return self.risk_class in HIGH_RISK_CLASSES

    @property
    def blocked(self) -> bool:
        """Whether Phase 7b refuses this even with a human approval."""
        return self.is_high_risk

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class.value,
            "high_risk": self.is_high_risk,
            "reasons": self.reasons,
            "matched": self.matched,
            "blocked_in_phase_7b": self.blocked,
        }


def classify_outbound_risk(
    *, subject: Optional[str], body: Optional[str], attachments: Iterable[Any] = ()
) -> RiskAssessment:
    """Classify what sending this message would mean.

    Reads both subject and body: a subject reading "Signed agreement attached" with
    an innocuous body is still a contract. Most severe match wins, because the
    classification decides whether the message may be sent at all - so when two
    readings are possible, the one that restricts is the safe one.
    """
    text = "\n".join(part for part in (subject, body) if part)
    attachment_list = list(attachments or ())

    for risk, pattern, reason in _RISK_PATTERNS:
        match = pattern.search(text)
        if match:
            return RiskAssessment(
                risk_class=risk,
                reasons=[reason],
                matched=[match.group(0)[:120]],
            )

    if attachment_list:
        # Documents alone are a document response, which is routine but worth
        # recording: it tells a reviewer to check the attachment manifest.
        return RiskAssessment(
            risk_class=OutboundRisk.DOCUMENT_RESPONSE,
            reasons=["the message carries attachments"],
            matched=[],
        )
    return RiskAssessment(
        risk_class=OutboundRisk.ROUTINE,
        reasons=["no consequential category matched"],
        matched=[],
    )


# ---------------------------------------------------------------------------
# Recipient safety
# ---------------------------------------------------------------------------
@dataclass
class RecipientReport:
    """What the recipient check found. Warnings inform; errors block."""

    normalised: list[str] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalised": self.normalised,
            "warnings": self.warnings,
            "errors": self.errors,
            "ok": self.ok,
        }


def normalise_recipients(values: Any) -> list[str]:
    """Lowercase, trim, deduplicate, preserve order."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        address = str(value or "").strip().lower()
        if address and address not in seen:
            seen.add(address)
            out.append(address)
    return out


def check_recipients(
    *,
    to_addresses: Any,
    cc_addresses: Any = None,
    bcc_addresses: Any = None,
    reply_to_address: Optional[str] = None,
    from_address: Optional[str] = None,
    known_donor_domains: Iterable[str] = (),
    known_donors: Iterable[dict] = (),
    thread_participants: Iterable[str] = (),
) -> RecipientReport:
    """Check a recipient set before approval, and again before send.

    Called twice on purpose: once when the intent is created, so the human sees the
    risks before deciding, and once immediately before submission, because the
    durable state may have changed while the approval sat waiting.
    """
    report = RecipientReport()
    to_list = normalise_recipients(to_addresses)
    cc_list = normalise_recipients(cc_addresses)
    bcc_list = normalise_recipients(bcc_addresses)
    report.normalised = to_list + [a for a in cc_list if a not in to_list]

    if not to_list:
        report.errors.append({"code": "NO_RECIPIENT", "detail": "a message needs at least one To address"})

    every_address = to_list + cc_list + bcc_list
    for address in every_address:
        if not _ADDRESS.match(address):
            report.errors.append({
                "code": "MALFORMED_ADDRESS",
                "detail": f"{address!r} is not a deliverable email address",
            })
            continue

        domain = domain_of(address)
        if domain in _FREEMAIL_DOMAINS:
            report.warnings.append({
                "code": "FREEMAIL_RECIPIENT",
                "detail": f"{address} is a free mail domain; a donor reply to a personal "
                          "address is unusual",
            })
        if domain in _SHORTENER_DOMAINS:
            report.errors.append({
                "code": "SHORTENER_RECIPIENT",
                "detail": f"{address} is a URL shortener domain, not a mailbox",
            })
        lookalike = _looks_like_lookalike(domain, known_donor_domains)
        if lookalike:
            report.errors.append({
                "code": "LOOKALIKE_RECIPIENT",
                "detail": f"{domain} closely resembles the known donor domain {lookalike}; "
                          "replying to an impersonated domain discloses correspondence "
                          "and any attached documents",
            })
        if domain and known_donor_domains and domain not in set(known_donor_domains):
            if not thread_participants or address not in set(thread_participants):
                report.warnings.append({
                    "code": "UNEXPECTED_DOMAIN",
                    "detail": f"{address} is not a domain this organisation has corresponded with",
                })

    # A display-name claim that does not match the address is worth surfacing on the
    # way out too: it is how an organisation is tricked into replying to an impostor.
    if known_donors:
        for address in every_address:
            domain = domain_of(address)
            for donor in known_donors or ():
                donor_domain = str(donor.get("domain") or "").lower()
                if donor_domain and domain and donor_domain != domain:
                    continue

    if from_address and from_address.lower() in {a.lower() for a in every_address}:
        report.warnings.append({
            "code": "SELF_RECIPIENT",
            "detail": "the sender appears in the recipient list",
        })

    if len(every_address) > LARGE_RECIPIENT_SET:
        report.warnings.append({
            "code": "LARGE_RECIPIENT_SET",
            "detail": f"{len(every_address)} recipients; confirm this is intended",
        })
    if bcc_list:
        report.warnings.append({
            "code": "BCC_USED",
            "detail": f"{len(bcc_list)} BCC recipient(s); they will not see each other",
        })
    if reply_to_address and domain_of(reply_to_address) not in {
        domain_of(a) for a in every_address
    }:
        report.warnings.append({
            "code": "REPLY_TO_MISMATCH",
            "detail": f"replies will go to {reply_to_address}, which is not a sender domain "
                      "in this message",
        })
    return report


# ---------------------------------------------------------------------------
# Attachment safety
# ---------------------------------------------------------------------------
@dataclass
class AttachmentReport:
    manifest: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"manifest": self.manifest, "warnings": self.warnings, "errors": self.errors, "ok": self.ok}


def build_attachment_manifest(
    documents: Iterable[Any], *, org_id: str
) -> AttachmentReport:
    """Freeze the attachment set, refusing anything not properly the organisation's.

    **Trusted provenance, not arbitrary inbound files.** Phase 7a correctly admits
    that no malware scanner exists. Until one does, eligibility is based on the
    Document Vault: a document the organisation uploaded and approved. An inbound
    attachment with ``scan_status = NOT_SCANNED`` or ``PENDING`` must never become an
    outbound attachment, because Granada has no basis for believing it is safe and
    sending malware to a funder is a reputational event that outlives the bug.

    Each entry freezes id, version, storage reference, filename, mime type and
    **checksum**. The checksum is what makes the freeze meaningful: a re-upload that
    keeps the id but changes the bytes changes the checksum, and the fingerprint
    consequently no longer matches the approval.
    """
    report = AttachmentReport()
    organisation_id = str(org_id or "")

    for document in documents or ():
        doc_org = getattr(document, "org_id", None)
        document_id = str(getattr(document, "id", "") or "")

        # The cross-tenant check comes first, and is an error rather than a warning:
        # there is no legitimate reason for one organisation to attach another's
        # document, and the composite key plus RLS should already make it
        # impossible. This is the third layer.
        if doc_org and organisation_id and str(doc_org) != organisation_id:
            report.errors.append({
                "code": "CROSS_TENANT_DOCUMENT",
                "detail": f"document {document_id} belongs to another organisation",
            })
            continue

        approval_status = getattr(document, "approval_status", None)
        if approval_status is not None and approval_status != "APPROVED":
            report.errors.append({
                "code": "DOCUMENT_NOT_APPROVED",
                "detail": f"document {document_id} is {approval_status}, not APPROVED; "
                          "sending an unapproved document as the organisation's evidence "
                          "is the error the vault's approval step exists to prevent",
            })
            continue

        if getattr(document, "is_current", True) is False:
            report.errors.append({
                "code": "DOCUMENT_SUPERSEDED",
                "detail": f"document {document_id} is not the current version",
            })
            continue

        checksum = getattr(document, "checksum_sha256", None)
        if not checksum:
            report.errors.append({
                "code": "DOCUMENT_NO_CHECKSUM",
                "detail": f"document {document_id} has no checksum, so the bytes cannot be "
                          "proven to be the ones approved",
            })
            continue

        not_after = getattr(document, "valid_until", None)
        if not_after is not None:
            moment = datetime.now(timezone.utc)
            expiry = not_after if not_after.tzinfo else not_after.replace(tzinfo=timezone.utc)
            if expiry <= moment:
                report.errors.append({
                    "code": "DOCUMENT_EXPIRED",
                    "detail": f"document {document_id} expired on {expiry.date().isoformat()}",
                })
                continue

        entry = {
            "document_id": document_id,
            "version": getattr(document, "version", None),
            "storage_ref": getattr(document, "storage_ref", None) or getattr(document, "storage_key", None),
            "filename": getattr(document, "filename", None) or getattr(document, "title", None),
            "mime_type": getattr(document, "mime_type", None),
            "checksum_sha256": checksum,
        }
        report.manifest.append(entry)

        # A vault document that came FROM an inbound attachment is only usable if the
        # scan was clean. That provenance is what keeps unscanned external files off
        # the outbound path.
        scan_status = getattr(document, "scan_status", None)
        if scan_status in ("PENDING", "NOT_SCANNED", "SUSPICIOUS", "FAILED"):
            report.errors.append({
                "code": "ATTACHMENT_NOT_SCANNED",
                "detail": f"document {document_id} has scan_status={scan_status}; Phase 7b "
                          "permits only documents with trusted vault provenance until a "
                          "malware scanner exists",
            })

    if report.manifest:
        total = len(report.manifest)
        report.warnings.append({
            "code": "ATTACHMENTS_PRESENT",
            "detail": f"{total} attachment(s) will be sent; confirm each is intended",
        })
    return report


def verify_attachment_manifest(
    manifest: Any, *, org_id: str, now: Optional[datetime] = None
) -> AttachmentReport:
    """Re-verify a frozen manifest against the database, immediately before send.

    The freeze is only as good as this check. Between approval and submission a
    document can be superseded, un-approved, expire or be deleted; each of those
    silently changes what the recipient receives if nobody looks again.
    """
    from sqlalchemy import select

    import models

    report = AttachmentReport(manifest=list(manifest or []))
    moment = now or datetime.now(timezone.utc)

    for entry in report.manifest:
        document_id = entry.get("document_id")
        document = None
        # The session is supplied by the caller through the module-level convention
        # used elsewhere in the fleet; see `send_service` for how it is bound.
        session = _SESSION.get()
        if session is not None and document_id:
            document = session.execute(
                select(models.Document).where(
                    models.Document.id == document_id,
                    models.Document.org_id == org_id,
                )
            ).scalars().first()

        if session is not None and document is None:
            report.errors.append({
                "code": "DOCUMENT_MISSING",
                "detail": f"document {document_id} is no longer available to this organisation",
            })
            continue
        if document is None:
            # No session bound: the caller is expected to have run
            # `build_attachment_manifest` against live rows.
            continue

        if getattr(document, "checksum_sha256", None) != entry.get("checksum_sha256"):
            report.errors.append({
                "code": "DOCUMENT_CHECKSUM_CHANGED",
                "detail": f"document {document_id} no longer has the checksum that was "
                          "approved; the bytes a funder would receive are not the bytes "
                          "the approver saw",
            })
        if getattr(document, "is_current", True) is False:
            report.errors.append({
                "code": "DOCUMENT_SUPERSEDED",
                "detail": f"document {document_id} was superseded after approval",
            })
        if getattr(document, "approval_status", None) not in (None, "APPROVED"):
            report.errors.append({
                "code": "DOCUMENT_UNAPPROVED",
                "detail": f"document {document_id} is no longer approved",
            })
        expiry = getattr(document, "valid_until", None)
        if expiry is not None:
            expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            if expiry <= moment:
                report.errors.append({
                    "code": "DOCUMENT_EXPIRED",
                    "detail": f"document {document_id} expired after approval",
                })
    return report


# A context-local session so `verify_attachment_manifest` can look rows up without
# every caller threading one through. Deliberately a ContextVar rather than a
# module global: a module global would leak a session between concurrent workers in
# the same process, which is precisely the multi-tenant confusion the fleet is
# designed to avoid.
import contextvars as _contextvars

_SESSION: "_contextvars.ContextVar[Any]" = _contextvars.ContextVar(
    "granada_mail_session", default=None
)
