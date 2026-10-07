"""Attaching an inbound message to the right application.

The rule that governs everything here, from the brief:

    Wrong linkage is worse than no linkage.

Why that is not a slogan
------------------------
A message linked to the wrong application produces a draft about the wrong grant,
quoting the wrong deadline, addressed to the wrong funder, and attaching the
evidence meant for a different donor. Every downstream step is then confidently
wrong, and nothing in the pipeline will notice - because the pipeline's whole job
is to act on the link it is given.

So the outcome is a **state with a reason**, not a best guess:

``EXACT``           an unambiguous machine identifier matched - a reply alias token
                    minted for one application, or a correlation header we set.
``HIGH_CONFIDENCE`` several independent signals agree, or a single strong one does.
``AMBIGUOUS``       two or more plausible applications, and nothing to separate
                    them. Pariked for a person.
``UNLINKED``        nothing matched. The message is kept safely and no
                    application-specific work happens.

Only ``EXACT`` and ``HIGH_CONFIDENCE`` may trigger application-specific work
autonomously. `CorrelationState.may_act_autonomously` is the single place that
decides, so the rule cannot be enforced in three places and violated in a fourth.

Signals, strongest first
-----------------------
The ordering is by *forgeability*, not by convenience. A reply alias token was
minted by Granada for one application and is unguessable; a subject line was
written by a human and is worth almost nothing. Sorting the other way round would
let a coincidental subject match outrank the identifier we control, which is how a
confident wrong answer gets produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import select

import models
from agent.mail.vocabulary import CorrelationState, LinkMethod

#: How much each signal is worth. Deliberately coarse: a precise weighting would
#: imply a calibration nobody has done, and the brief's own states are ordinal.
_SIGNAL_WEIGHT: dict[LinkMethod, float] = {
    LinkMethod.REPLY_ALIAS_TOKEN: 1.0,      # minted by us for one application
    LinkMethod.CORRELATION_HEADER: 0.98,    # set by us on the message we sent
    LinkMethod.INTERNET_MESSAGE_ID: 0.95,   # an exact id we recorded as sent
    LinkMethod.IN_REPLY_TO_CHAIN: 0.9,      # an exact id in the reference chain
    LinkMethod.THREAD_PROVIDER_ID: 0.85,    # a stable provider grouping
    LinkMethod.APPLICATION_REFERENCE: 0.75, # a funder's own reference number
    LinkMethod.KNOWN_DONOR_AND_SUBJECT: 0.45,
    LinkMethod.UNIQUE_OPEN_APPLICATION: 0.4,
    LinkMethod.NONE: 0.0,
}

#: At or above this, a single signal is enough for HIGH_CONFIDENCE without
#: corroboration, because the signal is one only Granada could have produced.
_STRONG_SIGNAL = 0.9
#: Below this, a signal is not evidence and is recorded without being acted on.
_WEAK_SIGNAL = 0.4

#: A funder's own reference number, which is often the most reliable thing in the
#: body. Kept narrow: a pattern that matched any long number would match grant
#: amounts and phone numbers.
_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:application|reference|proposal|grant)\s*(?:no\.?|number|id|ref\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{4,30})\b", re.I),
    re.compile(r"\bGRANADA-([A-Z0-9]{6,})\b"),
)

#: Our own correlation header, set on outbound mail in a later phase. Named here so
#: the reader path exists before the writer does, and so the format is fixed now.
CORRELATION_HEADER = "X-Granada-Application"


@dataclass
class CorrelationCandidate:
    """One application that might be the intended one, and why."""

    application_id: str
    opportunity_id: Optional[str]
    donor_name: Optional[str]
    signals: list[str] = field(default_factory=list)
    score: float = 0.0
    subject_similarity: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "opportunity_id": self.opportunity_id,
            "donor_name": self.donor_name,
            "signals": list(self.signals),
            "score": round(self.score, 3),
            "subject_similarity": round(self.subject_similarity, 3),
        }


@dataclass
class CorrelationResult:
    """The outcome, with everything needed to explain or correct it."""

    state: CorrelationState
    method: LinkMethod
    application_id: Optional[str] = None
    signals: dict[str, Any] = field(default_factory=dict)
    candidates: list[CorrelationCandidate] = field(default_factory=list)
    reason: str = ""

    @property
    def may_act_autonomously(self) -> bool:
        """The one place the autonomy rule is decided."""
        return self.state.may_act_autonomously and self.application_id is not None

    @property
    def needs_human(self) -> bool:
        return self.state == CorrelationState.AMBIGUOUS


def normalise_subject(subject: Optional[str]) -> str:
    """Strip reply/forward prefixes and list tags, then normalise.

    Used **only** for comparison and display. A normalised subject is not an
    identity: two funders both send "Application update", and a subject that is
    identical across two of the organisation's applications is exactly the
    ambiguous case the design must detect rather than resolve.
    """
    if not subject:
        return ""
    text = subject.strip()
    # Reply and forward prefixes, repeatedly and until stable. A fixed count was a
    # real bug: five nested "Re:" prefixes left one behind, so two messages in the
    # same conversation normalised differently and would fail to group.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"^\s*(re|fwd?|aw|sv|答复|回复)\s*(\[\d+\])?\s*:\s*", "", text, flags=re.I
        )
    # Mailing-list tags, which some lists repeat around the prefix.
    list_previous = None
    while list_previous != text:
        list_previous = text
        text = re.sub(r"^\s*\[[^\]]{1,40}\]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _subject_similarity(left: Optional[str], right: Optional[str]) -> float:
    """Token overlap between two normalised subjects, in ``[0, 1]``.

    Token overlap rather than character distance: "Child Protection Grant 2027
    update" and "Update re: Child Protection Grant 2027" share their meaningful
    tokens, and a character metric would score them far apart because of ordering.
    """
    a = set(normalise_subject(left).split())
    b = set(normalise_subject(right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_references(*, subject: Optional[str], body: Optional[str]) -> list[str]:
    """Funder reference numbers mentioned in a message."""
    text = "\n".join(part for part in (subject, body) if part)
    found: list[str] = []
    for pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1).strip().upper()
            if token not in found:
                found.append(token)
    return found


class ApplicationCorrelator:
    """Resolves a message to one of an organisation's applications."""

    def __init__(self, db: Any, *, org_id: str, agent_id: str) -> None:
        if not org_id or not agent_id:
            raise ValueError("correlation requires both an organisation and an agent")
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id

    # ------------------------------------------------------------------
    def correlate(
        self,
        *,
        sender: Optional[str],
        subject: Optional[str],
        body: Optional[str],
        in_reply_to: Optional[str] = None,
        references: Iterable[str] = (),
        provider_thread_id: Optional[str] = None,
        recipients: Iterable[str] = (),
        headers: Optional[dict[str, str]] = None,
    ) -> CorrelationResult:
        """Resolve one message. Never guesses; reports what it found."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        references = list(references or ())
        recipients = list(recipients or ())

        signals: dict[str, Any] = {
            "sender": sender,
            "normalised_subject": normalise_subject(subject),
            "in_reply_to": in_reply_to,
            "reference_chain_size": len(references),
            "provider_thread_id": provider_thread_id,
        }

        # -- 1. The reply-alias token. The strongest signal that exists. ----
        token_hit = self._by_reply_alias(recipients)
        if token_hit is not None:
            signals["reply_alias_token_matched"] = True
            return self._resolve(
                LinkMethod.REPLY_ALIAS_TOKEN, token_hit, signals,
                reason="the message arrived at an alias minted for this application",
            )
        signals["reply_alias_token_matched"] = False

        # -- 2. Our own correlation header. ---------------------------------
        header_value = headers.get(CORRELATION_HEADER.lower())
        if header_value:
            application = self._application(header_value)
            if application is not None:
                signals["correlation_header"] = header_value
                return self._resolve(
                    LinkMethod.CORRELATION_HEADER, application, signals,
                    reason="the message carried Granada's own correlation header",
                )

        # -- 3. A thread we have already linked. ----------------------------
        if provider_thread_id:
            thread = self._thread_by_provider_id(provider_thread_id)
            if thread is not None and thread.application_id:
                application = self._application(thread.application_id)
                if application is not None:
                    signals["thread_id"] = thread.id
                    return self._resolve(
                        LinkMethod.THREAD_PROVIDER_ID, application, signals,
                        reason="the provider thread is already linked to this application",
                    )

        # -- 4. The reference chain we recorded when we sent mail. ----------
        # We sent the message being replied to, so an id we stored for an
        # application is an exact match rather than an inference.
        for candidate_id in [in_reply_to, *references]:
            if not candidate_id:
                continue
            message = self._message_by_internet_id(candidate_id)
            if message is not None and message.thread_id:
                thread = self._thread(message.thread_id)
                if thread is not None and thread.application_id:
                    application = self._application(thread.application_id)
                    if application is not None:
                        signals["matched_message"] = candidate_id
                        return self._resolve(
                            LinkMethod.IN_REPLY_TO_CHAIN, application, signals,
                            reason="the reference chain contains a message we stored for this application",
                        )

        # -- 5. A funder's own reference number. ----------------------------
        for token in extract_references(subject=subject, body=body):
            application = self._by_reference(token)
            if application is not None:
                signals["application_reference"] = token
                return self._resolve(
                    LinkMethod.APPLICATION_REFERENCE, application, signals,
                    reason=f"the message quotes reference {token}",
                )

        # -- 6. Everything else is a guess, and guesses are ranked. --------
        return self._rank(
            sender=sender, subject=subject, signals=signals, headers=headers
        )

    # ------------------------------------------------------------------
    # Candidate collection
    # ------------------------------------------------------------------
    def _candidate_applications(self) -> list[CorrelationCandidate]:
        rows = self.db.execute(
            select(models.Application).where(models.Application.org_id == self.org_id)
        ).scalars().all()

        candidates: list[CorrelationCandidate] = []
        for application in rows:
            opportunity = self.db.execute(
                select(models.Opportunity).where(
                    models.Opportunity.id == application.opportunity_id
                )
            ).scalars().first()
            candidates.append(
                CorrelationCandidate(
                    application_id=application.id,
                    opportunity_id=application.opportunity_id,
                    donor_name=getattr(opportunity, "source_name", None),
                    subject_similarity=_subject_similarity(
                        getattr(opportunity, "title", None), None
                    ),
                )
            )
        return candidates

    def _rank(
        self,
        *,
        sender: Optional[str],
        subject: Optional[str],
        signals: dict[str, Any],
        headers: dict[str, str],
    ) -> CorrelationResult:
        """Score every plausible application, then decide whether a winner exists.

        The decision is deliberately conservative. A single plausible application
        is not enough if the message could plausibly belong to it *by coincidence*
        - and the test for that is whether the signals actually distinguish it,
        not how few candidates there are.
        """
        from agent.mail.security import domain_of

        candidates = self._candidate_applications()
        if not candidates:
            return CorrelationResult(
                state=CorrelationState.UNLINKED,
                method=LinkMethod.NONE,
                signals=signals,
                reason="this organisation has no applications to link to",
            )

        sender_domain = domain_of(sender)
        for candidate in candidates:
            opportunity = self.db.execute(
                select(models.Opportunity).where(
                    models.Opportunity.id == candidate.opportunity_id
                )
            ).scalars().first()
            if opportunity is None:
                continue

            # Subject similarity against the opportunity title.
            similarity = _subject_similarity(subject, opportunity.title)
            candidate.subject_similarity = similarity
            if similarity >= 0.6:
                candidate.signals.append(f"subject matches opportunity title ({similarity:.2f})")
                candidate.score += 0.5 * similarity
            elif similarity >= 0.3:
                candidate.signals.append(f"subject partially matches ({similarity:.2f})")
                candidate.score += 0.3 * similarity

            # Known donor: the message's domain appears in the opportunity's source
            # URL, which is how the funder was originally identified.
            source_url = (getattr(opportunity, "source_url", "") or "").lower()
            if sender_domain and sender_domain in source_url:
                candidate.signals.append(f"sender domain {sender_domain} matches the source")
                candidate.score += 0.6

            # A previous message in a thread we already linked.
            existing_links = self.db.execute(
                select(models.MailApplicationLink).where(
                    models.MailApplicationLink.org_id == self.org_id,
                    models.MailApplicationLink.application_id == candidate.application_id,
                    models.MailApplicationLink.status == models.MailApplicationLink.STATUS_ACTIVE,
                )
            ).scalars().all()
            if existing_links:
                candidate.signals.append(
                    f"{len(existing_links)} prior linked message(s) for this application"
                )
                candidate.score += min(0.4, 0.15 * len(existing_links))

        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        plausible = [c for c in ranked if c.score >= _WEAK_SIGNAL]
        signals["candidate_scores"] = {c.application_id: round(c.score, 3) for c in ranked[:5]}

        if not plausible:
            return CorrelationResult(
                state=CorrelationState.UNLINKED,
                method=LinkMethod.NONE,
                signals=signals,
                candidates=ranked,
                reason=(
                    "no signal connected this message to an application; it is kept "
                    "in the inbox and nothing application-specific is done"
                ),
            )

        if len(plausible) == 1 and plausible[0].score >= 0.6:
            # Exactly one application scored meaningfully. That is
            # HIGH_CONFIDENCE rather than EXACT: a subject and a donor domain
            # agreeing is strong, but it is inference rather than an identifier we
            # minted, and the states must not be conflated.
            return CorrelationResult(
                state=CorrelationState.HIGH_CONFIDENCE,
                method=LinkMethod.KNOWN_DONOR_AND_SUBJECT,
                application_id=plausible[0].application_id,
                signals=signals,
                candidates=ranked,
                reason="exactly one application scored meaningfully: " + "; ".join(plausible[0].signals),
            )

        if len(plausible) == 1:
            # One candidate, but only weak evidence for it. Not enough to act on,
            # and not ambiguous either - there is nothing to be ambiguous between.
            return CorrelationResult(
                state=CorrelationState.UNLINKED,
                method=LinkMethod.UNIQUE_OPEN_APPLICATION,
                signals=signals,
                candidates=ranked,
                reason=(
                    "one application exists but the evidence connecting this message "
                    f"to it is weak (score {plausible[0].score:.2f}); a person must link it"
                ),
            )

        # Two or more plausible, and nothing distinguishes them.
        return CorrelationResult(
            state=CorrelationState.AMBIGUOUS,
            method=LinkMethod.NONE,
            signals=signals,
            candidates=plausible,
            reason=(
                f"{len(plausible)} applications are plausible and no signal separates "
                "them; guessing would risk acting on the wrong grant"
            ),
        )

    def _resolve(
        self,
        method: LinkMethod,
        application: models.Application,
        signals: dict[str, Any],
        *,
        reason: str,
    ) -> CorrelationResult:
        """Build the result for an unambiguous identifier match.

        ``EXACT`` is reserved for signals Granada itself minted - the alias token,
        the correlation header, and ids we recorded when sending. Those cannot be
        produced by coincidence, unlike a subject or a sender name.
        """
        exact = method in (
            LinkMethod.REPLY_ALIAS_TOKEN,
            LinkMethod.CORRELATION_HEADER,
            LinkMethod.INTERNET_MESSAGE_ID,
            LinkMethod.IN_REPLY_TO_CHAIN,
        )
        return CorrelationResult(
            state=CorrelationState.EXACT if exact else CorrelationState.HIGH_CONFIDENCE,
            method=method,
            application_id=application.id,
            signals={**signals, "weight": _SIGNAL_WEIGHT.get(method, 0.0)},
            candidates=[
                CorrelationCandidate(
                    application_id=application.id,
                    opportunity_id=application.opportunity_id,
                    donor_name=None,
                    signals=[method.value],
                    score=_SIGNAL_WEIGHT.get(method, 0.0),
                )
            ],
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def _by_reply_alias(self, recipients: Iterable[str]) -> Optional[models.Application]:
        """Resolve a Granada reply alias back to its application.

        The token is what makes this exact: it was minted for one application, is
        unguessable, and carries no enumerable structure - so a funder replying to
        it identifies the grant without Granada inferring anything.
        """
        for address in recipients:
            local = (address or "").split("@")[0].strip().lower()
            if not local:
                continue
            identity = self.db.execute(
                select(models.MailIdentity).where(
                    models.MailIdentity.token == local,
                    models.MailIdentity.org_id == self.org_id,
                    models.MailIdentity.status == models.MailIdentity.ACTIVE,
                )
            ).scalars().first()
            if identity is None:
                continue
            if identity.revoked_at is not None:
                continue
            if identity.purpose_type == "APPLICATION" and identity.purpose_id:
                application = self._application(identity.purpose_id)
                if application is not None:
                    return application
        return None

    def _application(self, application_id: Optional[str]) -> Optional[models.Application]:
        """Always scoped by organisation: a foreign id resolves to nothing."""
        if not application_id:
            return None
        return self.db.execute(
            select(models.Application).where(
                models.Application.id == application_id,
                models.Application.org_id == self.org_id,
            )
        ).scalars().first()

    def _thread(self, thread_id: Optional[str]) -> Optional[models.MailThread]:
        if not thread_id:
            return None
        return self.db.execute(
            select(models.MailThread).where(
                models.MailThread.id == thread_id,
                models.MailThread.org_id == self.org_id,
            )
        ).scalars().first()

    def _thread_by_provider_id(self, provider_thread_id: str) -> Optional[models.MailThread]:
        return self.db.execute(
            select(models.MailThread).where(
                models.MailThread.provider_thread_id == provider_thread_id,
                models.MailThread.org_id == self.org_id,
            )
        ).scalars().first()

    def _message_by_internet_id(self, internet_message_id: str) -> Optional[models.MailMessage]:
        """Find one of OUR stored messages by its Internet Message-ID.

        Scoped by organisation and by account, per the brief: two tenants can both
        hold a message whose Message-ID is the same string - a forwarded message
        does exactly that - and deduplicating across tenants on that basis would
        merge two organisations' correspondence.
        """
        cleaned = internet_message_id.strip().strip("<>").lower()
        rows = self.db.execute(
            select(models.MailMessage).where(
                models.MailMessage.org_id == self.org_id,
                models.MailMessage.internet_message_id.isnot(None),
            )
        ).scalars().all()
        for message in rows:
            if (message.internet_message_id or "").strip().strip("<>").lower() == cleaned:
                return message
        return None

    def _by_reference(self, token: str) -> Optional[models.Application]:
        """Match a funder's reference number against stored application facts."""
        wanted = token.strip().upper()
        if not wanted:
            return None
        rows = self.db.execute(
            select(models.Application).where(models.Application.org_id == self.org_id)
        ).scalars().all()
        for application in rows:
            for attribute in ("funder_reference", "reference", "external_reference"):
                value = getattr(application, attribute, None)
                if value and str(value).strip().upper() == wanted:
                    return application
        return None
