"""Giving an approved NGO its own permanent @granadac.com address.

DECISION 1 FROM THE EMAIL DIRECTIVE: every approved NGO gets one permanent, friendly address at
onboarding, generated from its own name. Guessable is the POINT here - it is the organisation's
public mailbox, the address it puts on a grant application - which is why this is deliberately
separate from the opaque `MailIdentity.token` that backs per-application reply aliases. Those must
stay unguessable, because enumerating them would reveal which NGOs applied to which grants.

Thousands of NGOs must be supportable with no per-mailbox provisioning at SES or Cloudflare: the
catch-all delivers everything to one Worker, and the address is simply a row in this table.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional

DOMAIN = "granadac.com"

#: Words that must never be assigned to an NGO. These addresses carry operational meaning and are
#: exactly the ones an attacker would try first, or that a mail system needs to keep for itself.
RESERVED = frozenset({
    "admin", "administrator", "postmaster", "hostmaster", "webmaster", "abuse", "security",
    "noreply", "no-reply", "donotreply", "mailer-daemon", "mailerdaemon", "root", "bounce",
    "bounces", "dmarc", "spf", "dkim", "autodiscover", "smtp", "imap", "pop", "mail",
    "support", "help", "helpdesk", "billing", "accounts", "finance", "legal", "compliance",
    "privacy", "gdpr", "dpo", "sales", "marketing", "press", "media", "jobs", "careers",
    "hr", "recruitment", "verify", "verification", "confirm", "system", "notifications",
    "alerts", "info", "contact", "hello", "team", "office", "enquiries", "inquiries",
    "granada", "granadac", "general", "enquiry", "inquiry", "staff", "board", "trustees",
    "grants", "funding", "donations", "donate", "volunteer", "volunteers", "partnerships",
    "partners", "programmes", "programs", "projects", "operations", "reception",
    # Local parts that are not valid here for structural reasons.
    "www", "api", "app", "dashboard", "portal", "login", "signup", "register",
})

#: Legal-form words stripped from the local part, so "War Child Trust" and "War Child" do not become
#: two different addresses for one organisation.
_LEGAL_SUFFIXES = (
    "limited", "ltd", "llc", "llp", "inc", "incorporated", "plc", "gmbh", "bv", "nv",
    "sa", "sarl", "pty", "cc", "co", "company", "corp", "corporation",
    "ngo", "npo", "cbo", "cso", "fbo", "charity", "foundation", "trust", "association",
    "organisation", "organization", "society", "initiative", "network", "alliance",
    "group", "centre", "center", "project", "programme", "program", "uganda", "africa",
)

MIN_LOCAL, MAX_LOCAL = 3, 48


@dataclass(frozen=True)
class Allocation:
    address: str
    local_part: str
    reason: str


def slugify(name: str) -> str:
    """A stable, mail-safe local part derived from an organisation's own name.

    Stable matters: the address appears on grant applications, so re-deriving it must give the same
    answer. Every transformation here is deterministic.
    """
    text = (name or "").strip().lower()
    # Drop punctuation entirely rather than transliterating it, so "St. Mary's" and "St Marys"
    # converge instead of producing two addresses for one organisation.
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    parts = [p for p in text.split("-") if p]
    # Strip trailing legal-form words, repeatedly: "War Child Trust Limited" -> "war-child".
    while parts and parts[-1] in _LEGAL_SUFFIXES:
        parts.pop()
    slug = "-".join(parts).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:MAX_LOCAL].strip("-")


def _candidate(base: str, attempt: int) -> str:
    """The nth address to try. The first is the bare slug; later ones gain a short discriminator.

    A numeric tail (`war-child-2`) is chosen over random characters because it stays readable and
    typeable - the address is public and will be written on forms by hand.
    """
    if attempt == 0:
        return base
    return f"{base[: MAX_LOCAL - 4]}-{attempt + 1}"


def allocate(
    name: str,
    *,
    taken: Optional[set[str]] = None,
    domain: str = DOMAIN,
    max_attempts: int = 50,
) -> Allocation:
    """Choose the address for an organisation, given the local parts already in use.

    `taken` is passed in rather than queried, so this is pure and testable - and so the caller can
    supply the set scoped to the right thing. Uniqueness is global across the domain, not per
    organisation: two NGOs cannot share `war-child@granadac.com` however they are tenant-separated,
    because the domain has one namespace and Cloudflare routes one catch-all.
    """
    used = {t.strip().lower() for t in (taken or set())}
    base = slugify(name)

    if not base:
        # A name that is entirely punctuation or non-Latin. Rather than refusing - the NGO is real
        # and needs an address - derive an opaque but stable one. It cannot be derived from the name,
        # so it is random; the operator can rename it later.
        base = f"ngo-{secrets.token_hex(3)}"
        return Allocation(f"{base}@{domain}", base, "the name yielded no usable characters")

    if len(base) < MIN_LOCAL:
        base = f"{base}-ngo"

    if base in RESERVED:
        # Reserved for operational use. Suffixing keeps the organisation recognisable rather than
        # inventing an unrelated address it would not recognise on a form.
        base = f"{base}-ngo"

    for attempt in range(max_attempts):
        candidate = _candidate(base, attempt)
        if candidate in RESERVED:
            continue
        if candidate not in used and len(candidate) >= MIN_LOCAL:
            reason = (
                "derived from the organisation's name"
                if attempt == 0
                else f"derived from the name; {attempt} earlier form(s) were already in use"
            )
            return Allocation(f"{candidate}@{domain}", candidate, reason)

    raise ValueError(
        f"no free address for {name!r} after {max_attempts} attempts; "
        "the name collides unusually widely and needs operator attention"
    )


def taken_local_parts(db: Any, *, domain: str = DOMAIN) -> set[str]:
    """Every local part already assigned on this domain.

    Deliberately NOT tenant-scoped. The `mail_identities` UNIQUE constraint is on the whole address,
    so uniqueness is a property of the domain, and a per-tenant view would let two organisations
    collide at insert time. This reads only the local parts - no message content, no tenant data.
    """
    import models

    rows = db.query(models.MailIdentity.address).all()
    out: set[str] = set()
    for (address,) in rows:
        address = (address or "").strip().lower()
        if "@" in address:
            local, _, host = address.rpartition("@")
            if host == domain:
                out.add(local)
    return out


def provision(
    db: Any,
    *,
    org_id: str,
    agent_id: str,
    organisation_name: str,
    display_name: Optional[str] = None,
) -> Any:
    """Create the organisation's permanent managed address, idempotently.

    Returns the existing identity when the organisation already has a primary address, so onboarding
    that runs twice does not produce a second mailbox - and does not orphan the one a grant
    application already quotes.
    """
    import models

    existing = (
        db.query(models.MailIdentity)
        .filter(
            models.MailIdentity.org_id == org_id,
            models.MailIdentity.is_primary.is_(True),
        )
        .first()
    )
    if existing is not None:
        return existing

    allocation = allocate(organisation_name, taken=taken_local_parts(db))
    identity = models.MailIdentity(
        org_id=org_id,
        agent_id=agent_id,
        address=allocation.address,
        display_name=display_name or organisation_name,
        identity_type=models.MailIdentity.TYPE_MANAGED,
        is_primary=True,
        status=models.MailIdentity.ACTIVE,
        # The opaque token is what backs per-application reply aliases. Random and private: the
        # directory of aliases must not be enumerable, unlike the public mailbox above.
        token=secrets.token_urlsafe(24),
    )
    db.add(identity)
    db.flush()
    return identity


def describe() -> dict[str, Any]:
    return {
        "domain": DOMAIN,
        "reserved": sorted(RESERVED)[:10],
        "policy": "one permanent friendly address per approved NGO, derived from its name",
        "separation": "friendly public mailbox vs opaque per-application reply alias token",
    }
