"""Classifying inbound mail, and extracting the deadlines inside it.

Deterministic rules first, always
---------------------------------
The brief requires rule checks before any judgmental classification, and the
ordering is a safety property rather than a performance one. A rule that says
``if the body asks for bank details, this is a BANK_DETAIL_REQUEST`` cannot be
argued out of that conclusion by text inside the email. A model can.

So `classify_by_rules` runs first and its result is **final when it is
confident** - the model is only consulted for the residual, and its answer is
recorded as a judgment rather than as a rule hit. That is the model/policy split
the brief insists on: the model interprets, it does not authorise.

Deadlines
---------
A deadline extracted from correspondence is a commitment the organisation has
made, so it is persisted as durable work rather than living inside draft text. The
raw expression is kept alongside the resolution because "within five days" and
"by 16 October 2026" resolve differently and the resolution is an interpretation
that has to be auditable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

import models
from agent.mail.vocabulary import (
    DOCUMENT_TYPE_MAP,
    DocumentRequestType,
    MailClassification,
)

#: Assumed when a message names no timezone. Recorded on every deadline so the
#: assumption is visible rather than implicit - a deadline two hours out is still
#: a missed deadline.
DEFAULT_TIMEZONE_ASSUMPTION = "UTC"

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
    "twenty-one": 21, "thirty": 30, "sixty": 60, "ninety": 90,
}

# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------
# Each entry is (classification, compiled pattern). Ordered by consequence, not by
# likelihood: the most consequential reading wins when two could apply, because
# treating a bank-detail request as a general question is the expensive mistake.
#
# CLARIFICATION_REQUEST is deliberately ABOVE DOCUMENT_REQUEST. "We require
# further information about your budget" matches both, and the first version
# classified it as a document request - creating an obligation to produce a
# document nobody asked for. A clarification is a question, and the safer
# reading is the one that creates no obligation.
_RULES: tuple[tuple[MailClassification, re.Pattern[str], str], ...] = (
    (
        MailClassification.BANK_DETAIL_REQUEST,
        re.compile(
            r"\b(bank\s+(details?|account|coordinates)|account\s+number|iban|"
            r"swift\s+code|bic\b|routing\s+number|payment\s+details?|"
            r"remit\s+to|wire\s+(transfer|the\s+funds))\b",
            re.I,
        ),
        "banking details requested",
    ),
    (
        MailClassification.CONTRACT,
        re.compile(
            r"\b(grant\s+agreement|contract|memorandum\s+of\s+understanding|\bmou\b|"
            r"sign(ed|ature)?\s+(and\s+return|the\s+agreement)|countersign)\b",
            re.I,
        ),
        "contract or agreement referenced",
    ),
    (
        MailClassification.AWARD_NOTICE,
        re.compile(
            r"\b(we\s+are\s+pleased\s+to\s+(inform|confirm)|award(ed)?\s+(you|your)|"
            r"successful\s+(applicant|proposal)|grant\s+(has\s+been\s+)?approved|"
            r"congratulations)\b",
            re.I,
        ),
        "award language present",
    ),
    (
        MailClassification.REJECTION_NOTICE,
        re.compile(
            r"\b(unfortunately|regret\s+to\s+(inform|advise)|not\s+(been\s+)?"
            r"(successful|selected|shortlisted)|unsuccessful|declined)\b",
            re.I,
        ),
        "rejection language present",
    ),
    (
        MailClassification.CLARIFICATION_REQUEST,
        re.compile(
            r"\b(clarif(y|ication)|further\s+(information|details|documents)|"
            r"additional\s+(information|details|documents)|could\s+you\s+"
            r"(confirm|clarify|explain))\b",
            re.I,
        ),
        "clarification requested",
    ),
    (
        MailClassification.DOCUMENT_REQUEST,
        re.compile(
            r"\b(please\s+(provide|submit|send|share|upload)|kindly\s+(provide|submit|send)|"
            r"we\s+(require|need)|you\s+are\s+required\s+to\s+(provide|submit)|"
            r"provide\s+us\s+with|forward\s+(us\s+)?(a\s+copy|your))\b",
            re.I,
        ),
        "a document or information request is phrased as an instruction",
    ),
    (
        MailClassification.DEADLINE_CHANGE,
        re.compile(
            r"\b(deadline\s+(has\s+been\s+)?(extended|changed|moved|postponed)|"
            r"extended\s+to|new\s+deadline|extend(ed)?\s+the\s+deadline|"
            r"submission\s+deadline)\b",
            re.I,
        ),
        "deadline language present",
    ),
    (
        MailClassification.INTERVIEW_INVITATION,
        re.compile(
            r"\b(interview|panel\s+(discussion|meeting)|presentation\s+to\s+the\s+"
            r"(panel|committee)|due\s+diligence\s+(call|meeting)|schedule\s+a\s+call)\b",
            re.I,
        ),
        "interview or meeting proposed",
    ),
    (
        MailClassification.SHORTLIST_NOTICE,
        re.compile(r"\b(shortlist(ed)?|short\s+list|next\s+stage|second\s+stage)\b", re.I),
        "shortlist language present",
    ),
    (
        MailClassification.FINANCIAL_REQUEST,
        re.compile(
            r"\b(financial\s+(report|statement)s?\s+(are\s+)?(due|required|outstanding)|"
            r"audited\s+(accounts|financial)|invoice|disbursement|tranche)\b",
            re.I,
        ),
        "financial reporting or disbursement referenced",
    ),
    (
        MailClassification.ACKNOWLEDGEMENT,
        re.compile(
            r"\b(thank\s+you\s+for\s+(your\s+)?(application|submission|proposal)|"
            r"we\s+have\s+received\s+your|acknowledg(e|ing)\s+(receipt|your)|"
            r"this\s+is\s+to\s+confirm\s+we\s+received|successfully\s+submitted)\b",
            re.I,
        ),
        "receipt acknowledged",
    ),
    (
        MailClassification.BOUNCE,
        re.compile(
            r"\b(mail(er)?[- ]daemon|undeliverable|delivery\s+(has\s+)?failed|"
            r"address\s+not\s+found|message\s+(could\s+not|cannot)\s+be\s+delivered|"
            r"returned\s+to\s+sender|550\s+5\.1\.1)\b",
            re.I,
        ),
        "bounce or delivery failure",
    ),
    (
        MailClassification.AUTOMATED_NOTIFICATION,
        re.compile(
            r"\b(do\s+not\s+reply|automated\s+(message|notification|response)|"
            r"this\s+is\s+an\s+automatic(al|)|\bno-?reply\b)\b",
            re.I,
        ),
        "an automated sender",
    ),
    (
        MailClassification.APPLICATION_STATUS,
        re.compile(
            r"\b(status\s+of\s+your\s+application|application\s+(is\s+)?(under\s+review|"
            r"in\s+progress|being\s+processed)|your\s+application\s+reference)\b",
            re.I,
        ),
        "application status referenced",
    ),
    (
        MailClassification.GENERAL_QUESTION,
        re.compile(
            r"\?|\b(could\s+you\s+(tell|let\s+us\s+know)|would\s+you\s+be\s+able|"
            r"please\s+(advise|confirm|let\s+us\s+know))\b",
            re.I,
        ),
        "a question without a specific request",
    ),
)


@dataclass
class ClassificationResult:
    """One classification, with everything needed to explain it."""

    classification: MailClassification
    method: str
    confidence: float
    rule_hits: list[dict[str, str]] = field(default_factory=list)
    #: The document kind, when the message is a document request.
    document_request: Optional[DocumentRequestType] = None
    detail: str = ""

    @property
    def is_document_request(self) -> bool:
        return self.classification == MailClassification.DOCUMENT_REQUEST

    @property
    def is_sensitive(self) -> bool:
        from agent.mail.vocabulary import SENSITIVE_CLASSIFICATIONS

        return self.classification.value in SENSITIVE_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# Document-request identification
# ---------------------------------------------------------------------------
# Ordered by specificity: "audited financial statements" must beat "financial
# statements", or a request for audited accounts would be satisfied by an
# unaudited one.
_DOCUMENT_PATTERNS: tuple[tuple[DocumentRequestType, re.Pattern[str]], ...] = (
    (
        DocumentRequestType.AUDITED_FINANCIAL_STATEMENTS,
        re.compile(
            r"\b(audited\s+(financial\s+)?(statement|account|report)s?|"
            r"latest\s+audit(ed)?\s+(account|statement|report)s?|"
            r"audit(ed)?\s+financials?)\b",
            re.I,
        ),
    ),
    (DocumentRequestType.TAX_CLEARANCE, re.compile(r"\b(tax\s+(clearance|compliance|certificate)|tin\s+certificate)\b", re.I)),
    (DocumentRequestType.REGISTRATION_CERTIFICATE, re.compile(r"\b(certificate\s+of\s+(registration|incorporation)|registration\s+certificate|certificate\s+of\s+good\s+standing)\b", re.I)),
    (DocumentRequestType.SAFEGUARDING_POLICY, re.compile(r"\b(safeguarding|child\s+protection)\s+polic(y|ies)\b", re.I)),
    (DocumentRequestType.INSURANCE_CERTIFICATE, re.compile(r"\b(insurance\s+(certificate|policy)|liability\s+cover)\b", re.I)),
    (DocumentRequestType.BANK_CONFIRMATION, re.compile(r"\b(bank\s+(confirmation|letter|details\s+letter)|confirmation\s+of\s+bank)\b", re.I)),
    (DocumentRequestType.ANNUAL_REPORT, re.compile(r"\b(annual\s+report|yearly\s+report|latest\s+annual)\b", re.I)),
    (DocumentRequestType.ORGANISATION_PROFILE, re.compile(r"\b(organisation|organization|company)\s+profile\b", re.I)),
    (DocumentRequestType.LOGFRAME, re.compile(r"\b(log\s*frame|logframe|results\s+framework)\b", re.I)),
    (DocumentRequestType.BUDGET, re.compile(r"\b(budget|financial\s+plan)\b", re.I)),
    (DocumentRequestType.PROJECT_PROPOSAL, re.compile(r"\b(proposal|project\s+plan|concept\s+note)\b", re.I)),
    (DocumentRequestType.REFERENCE_LETTER, re.compile(r"\b(reference\s+letter|letter\s+of\s+support|recommendation\s+letter)\b", re.I)),
)


def identify_document_request(text: str) -> DocumentRequestType:
    """Which standing document a request is asking for.

    **The earliest mention wins**, with specificity breaking ties. A message saying
    "send your registration certificate and tax clearance" asks for two documents,
    and the first one named is the one being asked for. The first version returned
    the most *specific* pattern instead, which reported tax clearance for a message
    that mentioned registration first - technically defensible and not what the
    funder asked for.

    Returns ``UNKNOWN`` rather than guessing. An unrecognised request becomes a data
    requirement for a person, which is the honest outcome - attaching an unrelated
    document to a funder is worse than saying we do not have it.
    """
    best_position: Optional[int] = None
    best_kind = DocumentRequestType.UNKNOWN
    for kind, pattern in _DOCUMENT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        position = match.start()
        # Earlier wins; at the same position the more specific pattern wins, which
        # is why the tuple is ordered by specificity.
        if best_position is None or position < best_position:
            best_position = position
            best_kind = kind
    return best_kind


#: A phrase that makes the request explicit enough to act on.
_EXPLICIT_REQUEST = re.compile(
    r"\b(please\s+(provide|submit|send|share|upload|forward)|kindly\s+(provide|submit|send)|"
    r"we\s+(require|need|request)|you\s+(are\s+)?(required|requested)\s+to|"
    r"provide\s+us\s+with)\b",
    re.I,
)


def classify_by_rules(
    *, subject: Optional[str], body: Optional[str], has_attachments: bool = False
) -> Optional[ClassificationResult]:
    """Deterministic classification. Returns ``None`` when no rule is confident.

    Returning ``None`` rather than defaulting to ``UNKNOWN`` matters: it is what
    lets the caller distinguish "the rules found nothing, ask the model" from "the
    model also found nothing, so this is genuinely UNKNOWN".
    """
    text = "\n".join(part for part in (subject, body) if part)
    if not text.strip():
        return None

    hits: list[dict[str, str]] = []
    matched: Optional[MailClassification] = None
    for classification, pattern, reason in _RULES:
        found = pattern.search(text)
        if found:
            hits.append({
                "classification": classification.value,
                "reason": reason,
                "match": found.group(0)[:120],
            })
            if matched is None:
                # Rules are ordered by consequence, so the first match is the one
                # that must win.
                matched = classification

    if matched is None:
        return None

    # A DOCUMENT_REQUEST is only actionable when the request is explicit AND a
    # document kind is identifiable. "We may need something at some point" is not
    # a request, and treating it as one creates a deadline that does not exist.
    if matched == MailClassification.DOCUMENT_REQUEST:
        document_request = identify_document_request(text)
        explicit = bool(_EXPLICIT_REQUEST.search(text))
        if not explicit and document_request == DocumentRequestType.UNKNOWN:
            return None  # genuinely ambiguous; hand it to judgment, then a person
        confidence = 0.95 if (explicit and document_request != DocumentRequestType.UNKNOWN) else 0.7
        return ClassificationResult(
            classification=matched,
            method="RULE",
            confidence=confidence,
            rule_hits=hits,
            document_request=document_request,
            detail=f"document request for {document_request.value}",
        )

    # Two or more rules agreeing is as much confidence as rules can produce.
    agreeing = sum(1 for hit in hits if hit["classification"] == matched.value)
    confidence = 0.9 if agreeing > 1 else 0.75
    # A rejection or an award is a material outcome. The patterns are strong
    # enough to act on, but a person should see them before anything follows.
    if matched in (MailClassification.AWARD_NOTICE, MailClassification.REJECTION_NOTICE):
        confidence = 0.8
    return ClassificationResult(
        classification=matched,
        method="RULE",
        confidence=confidence,
        rule_hits=hits,
        detail=hits[0]["reason"] if hits else "",
    )


# ---------------------------------------------------------------------------
# Deadline extraction
# ---------------------------------------------------------------------------
_RELATIVE_DEADLINE = re.compile(
    r"\b(?:within|in|no\s+later\s+than|at\s+least)\s+"
    r"(?P<count>\d{1,3}|" + "|".join(_WORD_NUMBERS) + r")\s*"
    r"(?P<unit>hour|day|week|month|year|working\s+day|business\s+day)s?\b",
    re.I,
)
_EXPLICIT_DEADLINE = re.compile(
    r"\b(?:by|before|on|no\s+later\s+than|due(?:\s+(?:by|on))?|deadline(?:\s+(?:is|of))?|"
    r"closing\s+date(?:\s+is)?)\s*:?\s*"
    r"(?P<day>\d{1,2})(?:\s*(?:st|nd|rd|th))?\s+"
    r"(?P<month>[A-Za-z]{3,9})\.?\s*(?P<year>\d{4})?",
    re.I,
)
_ISO_DEADLINE = re.compile(
    r"\b(?:by|before|on|deadline(?:\s+is)?)\s*:?\s*"
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b",
    re.I,
)
_TIME_OF_DAY = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm|hrs?|hours?)?\b"
    r"(?:\s*(?P<tz>UTC|GMT|EAT|BST|CET|EST|PST|EEST))?",
    re.I,
)


@dataclass
class DeadlineResult:
    """A deadline as extracted, with the interpretation kept separate."""

    raw_expression: str
    resolved_at: Optional[datetime]
    timezone_assumption: str
    confidence: float
    resolved_by: str
    #: True when the text names a deadline but it cannot be pinned down. Surfaced
    #: rather than dropped, because a deadline nobody resolved is still a deadline.
    ambiguous: bool = False
    detail: str = ""

    @property
    def status(self) -> str:
        if self.resolved_at is not None:
            return "RESOLVED"
        return "AMBIGUOUS" if self.ambiguous else "UNRESOLVED"


def _word_to_number(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def extract_deadline(
    *, subject: Optional[str], body: Optional[str], now: Optional[datetime] = None
) -> Optional[DeadlineResult]:
    """Find and resolve a deadline. Returns ``None`` when the text names none.

    ``now`` is injected rather than read from the clock, so a test can assert an
    exact resolution and the production path is the only caller that uses the real
    time. A deadline resolved against a hidden clock is a deadline nobody can test.
    """
    moment = now or datetime.now(timezone.utc)
    text = "\n".join(part for part in (subject, body) if part)
    if not text.strip():
        return None

    # 1. Relative: "within five days". Resolved against the message time, which is
    #    the only defensible anchor - resolving against "now" would move the
    #    deadline every time the message were reprocessed.
    relative = _RELATIVE_DEADLINE.search(text)
    if relative:
        count = _word_to_number(relative.group("count"))
        unit = relative.group("unit").lower().replace(" ", "_")
        if count is not None:
            # Working days skip weekends. An organisation told "five working days"
            # has five working days, and treating that as five calendar days would
            # report a deadline two days early - which is safe but wrong, and wrong
            # deadlines erode trust in all of them.
            if unit in ("working_day", "business_day"):
                resolved = _add_working_days(moment, count)
            else:
                resolved = moment + _timedelta_for(count, unit)
            return DeadlineResult(
                raw_expression=relative.group(0),
                resolved_at=resolved,
                timezone_assumption=DEFAULT_TIMEZONE_ASSUMPTION,
                confidence=0.95,
                resolved_by="rule:relative",
                detail=f"{count} {unit} from {moment.isoformat()}",
            )

    # 2. Absolute with a month name: "by 16 October 2026".
    absolute = _EXPLICIT_DEADLINE.search(text)
    if absolute:
        month = _MONTHS.get(absolute.group("month").lower()[:4].rstrip("."))
        if month is None:
            month = _MONTHS.get(absolute.group("month").lower()[:3])
        if month is not None:
            year = int(absolute.group("year")) if absolute.group("year") else moment.year
            day = int(absolute.group("day"))
            resolved, adjust = _safe_date(year, month, day, moment)
            # Default to end of business rather than midnight: a deadline of
            # "16 October" is not missed at 00:01 on the 16th.
            resolved = resolved.replace(hour=23, minute=59)
            confidence = 0.9 if absolute.group("year") else 0.7
            detail = f"parsed {absolute.group(0)!r}"
            if adjust:
                detail += f"; {adjust}"
            return DeadlineResult(
                raw_expression=absolute.group(0),
                resolved_at=resolved,
                timezone_assumption=DEFAULT_TIMEZONE_ASSUMPTION,
                # A year omitted means the resolver inferred one, and that
                # inference is the likeliest way this is wrong.
                confidence=confidence,
                resolved_by="rule:absolute",
                detail=detail,
            )

    # 3. ISO date.
    iso = _ISO_DEADLINE.search(text)
    if iso:
        try:
            resolved = datetime(
                int(iso.group("year")), int(iso.group("month")), int(iso.group("day")),
                23, 59, tzinfo=timezone.utc,
            )
        except ValueError:
            return DeadlineResult(
                raw_expression=iso.group(0), resolved_at=None,
                timezone_assumption=DEFAULT_TIMEZONE_ASSUMPTION, confidence=0.0,
                resolved_by="rule:iso", ambiguous=True,
                detail=f"{iso.group(0)!r} is not a real calendar date",
            )
        return DeadlineResult(
            raw_expression=iso.group(0), resolved_at=resolved,
            timezone_assumption=DEFAULT_TIMEZONE_ASSUMPTION, confidence=0.95,
            resolved_by="rule:iso", detail="ISO date",
        )

    # 4. Deadline language with no resolvable date. Surfaced, never dropped.
    vague = re.search(
        r"\b(deadline|due\s+date|closing\s+date|as\s+soon\s+as\s+possible|"
        r"urgent(ly)?|immediately|at\s+the\s+earliest)\b",
        text,
        re.I,
    )
    if vague:
        return DeadlineResult(
            raw_expression=vague.group(0),
            resolved_at=None,
            timezone_assumption=DEFAULT_TIMEZONE_ASSUMPTION,
            confidence=0.3,
            resolved_by="rule:vague",
            ambiguous=True,
            detail="deadline language with no resolvable date; a person must decide",
        )
    return None


def _timedelta_for(count: int, unit: str) -> timedelta:
    return {
        "hour": timedelta(hours=count),
        "day": timedelta(days=count),
        "week": timedelta(weeks=count),
        "month": timedelta(days=30 * count),
        "year": timedelta(days=365 * count),
    }.get(unit, timedelta(days=count))


def _add_working_days(moment: datetime, count: int) -> datetime:
    """Add ``count`` working days, skipping Saturday and Sunday.

    Public holidays are deliberately not modelled: they differ per country, and a
    wrong holiday calendar would silently move a deadline. Weekends are universal
    enough to be safe.
    """
    remaining = count
    cursor = moment
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _safe_date(year: int, month: int, day: int, moment: datetime) -> tuple[datetime, str]:
    """Build a date, rolling an impossible day and reporting the adjustment.

    ``31 February`` arrives from real mail. Failing the whole extraction over it
    would lose the deadline; silently using 28 February would be a quiet
    fabrication. So it clamps and *says so* in the detail.
    """
    try:
        return datetime(year, month, day, tzinfo=timezone.utc), ""
    except ValueError:
        import calendar

        last = calendar.monthrange(year, month)[1]
        clamped = min(day, last)
        return (
            datetime(year, month, clamped, tzinfo=timezone.utc),
            f"day {day} does not exist in {year}-{month:02d}; clamped to {clamped}",
        )


# ---------------------------------------------------------------------------
# Document lookup
# ---------------------------------------------------------------------------
@dataclass
class DocumentLookup:
    """Whether the organisation holds the document being asked for."""

    request_type: DocumentRequestType
    satisfied: bool
    document: Any = None
    candidates_considered: int = 0
    reason: str = ""


def find_approved_document(
    vault: Any, request_type: DocumentRequestType, *, now: Optional[datetime] = None
) -> DocumentLookup:
    """Find a *current, approved, unexpired* document satisfying a request.

    Delegates to ``DocumentVault.usable()`` rather than querying the table, because
    that method already encodes the conditions and re-deriving them here would
    create a second definition that could drift from the one the submission path
    uses. Each condition rules out a specific real mistake:

    * **approved** - an unapproved upload is a draft the organisation has not stood
      behind. Sending one to a funder as an official document is the error this
      check exists to prevent.
    * **current** - a superseded version is not the latest audited accounts.
    * **unexpired** - a registration certificate that lapsed last month is not
      evidence of anything current.

    Matching goes through the vault's own ``doc_type`` rather than a fuzzy title,
    so "audited financial statements" cannot be satisfied by a document merely
    called "accounts".

    A request type with no mapping is deliberately unsatisfiable. A proposal or a
    budget is application-specific work rather than a standing organisation
    document, so Granada must not reach for one.
    """
    wanted = DOCUMENT_TYPE_MAP.get(request_type, ())
    if not wanted:
        return DocumentLookup(
            request_type=request_type,
            satisfied=False,
            reason=(
                f"{request_type.value} has no standing-document mapping; it is "
                "application-specific work and must not be substituted"
            ),
        )

    considered = 0
    for doc_type in wanted:
        documents = vault.usable(doc_type=doc_type, now=now)
        for document in documents:
            considered += 1
            return DocumentLookup(
                request_type=request_type,
                satisfied=True,
                document=document,
                candidates_considered=considered,
                reason=f"approved, current {document.doc_type} found",
            )

    # Report what the vault does hold, so the human requirement can say whether the
    # organisation needs to upload something or to approve something it already
    # uploaded. Those are different jobs and conflating them wastes a round trip.
    unapproved = 0
    for doc_type in wanted:
        rows = vault.db.execute(
            select(models.Document).where(
                models.Document.org_id == vault.org_id,
                models.Document.doc_type == doc_type,
                models.Document.is_current.is_(True),
            )
        ).scalars().all()
        unapproved += sum(
            1 for row in rows if row.approval_status != models.Document.APPROVED
        )

    if unapproved:
        reason = (
            f"{unapproved} current document(s) matching {wanted} exist but none is "
            "approved; the organisation must approve one rather than upload another"
        )
    else:
        reason = f"no current approved document matching {wanted} in the vault"
    return DocumentLookup(
        request_type=request_type,
        satisfied=False,
        candidates_considered=considered,
        reason=reason,
    )
