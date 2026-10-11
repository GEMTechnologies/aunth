"""Creating and holding the account Granada registers on a portal's behalf.

THE ONE RULE THAT MATTERS: A PASSWORD IS GENERATED ONCE AND NEVER AGAIN.

A portal account is reachable only by its password. If a second run generated a fresh one, the first
would silently become the only way in - and it would exist nowhere. So the credential is stored BEFORE
it is used, and every later run reads it back rather than making a new one. `ensure_account` is
idempotent, and the stored record is the authority, not anything the caller holds in memory.

Credentials live in the existing encrypted `credential_secrets` store under one ref per
(organisation, portal). They are never written to a log, a task payload that outlives the run, or an
activity record - the worker receives them in memory for the length of one run, and the output
scrubber is told about them so they cannot leak through a traceback either.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

#: Characters that survive being written down, read aloud and typed from a screenshot. Ambiguous
#: glyphs are excluded deliberately: a password nobody can transcribe is a support burden, and this
#: one may have to be read off a dashboard by a human.
_ALPHABET = "".join(
    c for c in (string.ascii_letters + string.digits + "!@#$%^&*-_=+")
    if c not in "O0oIl1|`'\"\\"
)

DEFAULT_LENGTH = 20

#: The names portals give password fields. Used to decide what to type, and nothing else.
PASSWORD_FIELD_NAMES = (
    "password", "Password", "PASSWORD", "passwd", "pass", "pwd",
    "user_password", "userPassword", "new_password", "newPassword",
    "UN_PASSWORD_1", "HRS_APPL_WRK_HRS_CONFMPSWD",
)


def generate_password(length: int = DEFAULT_LENGTH) -> str:
    """A strong password with no ambiguous glyphs.

    `secrets.choice` rather than `random`: this is a credential, and a predictable one defeats the
    encryption it is stored under. Every class is guaranteed present, because a portal that requires
    a digit and a symbol rejects an otherwise-strong password and the failure looks like a bug.
    """
    if length < 12:
        raise ValueError("a generated portal password must be at least 12 characters")
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(c in "!@#$%^&*-_=+" for c in candidate)):
            return candidate


def account_ref(portal_host: str) -> str:
    """One credential per (organisation, portal) - never one per run.

    Keyed on the registrable-ish host as given, so `www.unpartnerportal.org` and
    `unpartnerportal.org` must be normalised by the caller into one value before they reach here; two
    refs for one account would mean two passwords for it.
    """
    host = (portal_host or "").strip().lower()
    if not host:
        raise ValueError("a portal account needs a host")
    return f"portal-account:{host}"


@dataclass(frozen=True)
class PortalAccount:
    email: str
    password: str
    portal_host: str
    created_at: Optional[str] = None
    #: True when this call created the credential, False when it read an existing one. An operator
    #: needs to tell "we just registered" from "we are logging in again".
    created_now: bool = False

    def values(self) -> dict[str, str]:
        """The names to offer this password under, for a form that asks for one."""
        return {name: self.password for name in PASSWORD_FIELD_NAMES}


def ensure_account(
    store: Any,
    *,
    org_id: str,
    portal_host: str,
    email: str,
    length: int = DEFAULT_LENGTH,
) -> PortalAccount:
    """Return the account for this organisation and portal, creating it only if absent.

    The email is recorded alongside the password so the credential is self-describing: a stored
    password with no idea which address it belongs to cannot be used by anyone.
    """
    if not org_id:
        raise ValueError("an account must belong to an organisation")
    if not email:
        raise ValueError("an account must record the address it was registered with")

    ref = account_ref(portal_host)
    # `CredentialStore.get` RAISES when there is nothing stored, rather than returning None - so the
    # absence that means "create one" arrives as an exception, and treating it as a failure would
    # make a first registration impossible.
    existing = None
    try:
        existing = store.get(org_id=org_id, ref=ref)
    except Exception:
        existing = None
    if existing:
        # IDEMPOTENT. Reading it back is the whole point: generating here would orphan the account.
        return PortalAccount(
            email=str(existing.get("email") or email),
            password=str(existing.get("password") or ""),
            portal_host=portal_host,
            created_at=str(existing.get("created_at") or "") or None,
            created_now=False,
        )

    account = PortalAccount(
        email=email,
        password=generate_password(length),
        portal_host=portal_host,
        created_at=datetime.now(timezone.utc).isoformat(),
        created_now=True,
    )
    # STORED BEFORE IT IS USED. A password that exists only in this process is one a crash loses,
    # along with the account it opens.
    store.put(
        org_id=org_id,
        ref=ref,
        payload={"email": account.email, "password": account.password,
                 "created_at": account.created_at},
        kind="portal_account",
        note=f"registration account for {portal_host}",
    )
    return account


def describe() -> dict[str, Any]:
    return {
        "length": DEFAULT_LENGTH,
        "alphabet_excludes": "O0oIl1|`'\"\\",
        "idempotency": "one credential per (org, portal host); never regenerated",
        "storage": "encrypted credential_secrets, read back on every later run",
    }
