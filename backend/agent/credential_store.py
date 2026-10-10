"""Encrypted credential storage, so a connection has somewhere to keep its secret.

WHY THIS EXISTS

`MailAccount.credentials_ref` has pointed at a secret store since the mail schema was written, and
there was never one to point at. So an OAuth access token had nowhere to live, and a per-organisation
IMAP password was impossible by construction - which is why mail could be configured for a deployment
and never for an organisation.

THE RULE THIS DOES NOT BREAK

    "No provider password is ever stored. There is no column for one, and `credentials_ref` points at
     the secret store rather than holding a secret."

`ciphertext` holds an authenticated-encryption token, not a password. It is useless without the key,
which comes from the environment and is never written to the database. A backup, a replica, or a
`pg_dump` in somebody's home directory yields nothing readable.

WHAT THE KEY MUST BE

A Fernet key: 32 url-safe base64 bytes. `credential_encryption_key` has NO working default, on purpose.
A default would mean a deployment that never set one still encrypts - with a key that is in the source
tree, which is not encryption. The store refuses to construct without one, so the failure is a startup
error rather than a false sense of safety.

WHY ENCRYPTION IS NOT THE WHOLE STORY

A secret is only as safe as the number of places it is written down. So this module:

  * never logs a payload, a ciphertext, or a key - `__repr__` is overridden to prove it
  * returns a COPY, so a caller that mutates the result cannot alter what the next caller reads
  * scopes every read by `org_id`, so a guessed `ref` from another tenant resolves to nothing
  * distinguishes "not found" from "found but undecryptable", because the first is a configuration
    state and the second is a key-rotation incident that must page someone
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

#: The setting that holds the Fernet key. No default that works - see the module docstring.
ENCRYPTION_KEY_SETTING = "credential_encryption_key"


class CredentialStoreError(RuntimeError):
    """Anything this module refuses to do."""


class CredentialStoreUnavailable(CredentialStoreError):
    """No usable key. A deployment state, and a deliberate one."""


class CredentialNotFound(CredentialStoreError):
    """No active credential under that (organisation, ref)."""


class CredentialUndecryptable(CredentialStoreError):
    """The credential exists but cannot be decrypted.

    Separate from `CredentialNotFound` because the responses differ: a missing credential is a
    configuration state an operator fixes, while this is a key-rotation incident - the key changed, or
    the row was tampered with, and every credential encrypted under the old key is now unreadable. That
    must be loud.
    """


@dataclass(frozen=True)
class CredentialRecord:
    """What a caller learns about a stored credential, without the secret.

    Deliberately excludes the payload. An operator listing connections should not be able to print
    credentials by accident, so the listing type has nowhere to put one.
    """

    ref: str
    kind: str
    status: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    rotated_at: Optional[datetime]
    note: Optional[str]


class CredentialStore:
    """Read and write one organisation's encrypted credentials."""

    def __init__(self, db: Session, *, key: str) -> None:
        if not key:
            raise CredentialStoreUnavailable(
                f"{ENCRYPTION_KEY_SETTING} is not set. Generate one with "
                "`python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"` and set it in the environment. A default '
                "key would mean this deployment encrypts with a value that is in the source tree."
            )
        try:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001 - any construction failure is the same problem
            raise CredentialStoreUnavailable(
                f"{ENCRYPTION_KEY_SETTING} is not a valid Fernet key (32 url-safe base64 bytes)"
            ) from exc
        self.db = db

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def put(
        self,
        *,
        org_id: str,
        ref: str,
        payload: dict[str, Any],
        kind: str = models.CredentialSecret.KIND_API_KEY,
        note: Optional[str] = None,
    ) -> CredentialRecord:
        """Store or replace the credential at `(org_id, ref)`.

        Replacing RATHER than inserting a second row: two active credentials under one ref would make
        "which one is in use" unanswerable, and the answer would change depending on row order.
        """
        if not org_id:
            raise CredentialStoreError("a credential must belong to an organisation")
        if not ref:
            raise CredentialStoreError("a credential must have a ref")
        if not payload:
            raise CredentialStoreError("refusing to store an empty credential payload")

        existing = self._row(org_id, ref)
        now = datetime.now(timezone.utc)
        token = self._encrypt(payload)

        if existing is None:
            existing = models.CredentialSecret(
                id=_new_id(),
                org_id=org_id,
                ref=ref,
                kind=kind,
                ciphertext=token,
                status=models.CredentialSecret.ACTIVE,
                created_at=now,
                note=note,
            )
            self.db.add(existing)
        else:
            existing.ciphertext = token
            existing.status = models.CredentialSecret.ACTIVE
            existing.updated_at = now
            # Kept, not overwritten: "when did the previous one stop working" stays answerable.
            existing.rotated_at = now
            if note is not None:
                existing.note = note

        self.db.flush()
        logger.info("credential.stored", extra={"credential_ref": ref, "kind": kind})
        return _record(existing)

    def revoke(self, *, org_id: str, ref: str) -> bool:
        """Mark a credential revoked. Returns whether anything changed.

        The ciphertext is DESTROYED rather than merely flagged. A revoked credential that can still be
        decrypted is one a bug can resurrect, and "revoked" that depends on every reader checking the
        flag is not revocation - it is a convention.
        """
        row = self._row(org_id, ref)
        if row is None:
            return False
        row.ciphertext = ""
        row.status = models.CredentialSecret.REVOKED
        row.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        logger.info("credential.revoked", extra={"credential_ref": ref})
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def get(self, *, org_id: str, ref: str) -> dict[str, Any]:
        """The credential at `(org_id, ref)`, decrypted. A COPY.

        A copy because a caller that mutates what it is handed would otherwise change what the next
        caller receives, and a credential that varies by call order is a bug nobody would find.
        """
        row = self._row(org_id, ref)
        if row is None or row.status != models.CredentialSecret.ACTIVE or not row.ciphertext:
            raise CredentialNotFound(f"no active credential for ref {ref!r}")
        return self._decrypt(row.ciphertext)

    def describe(self, *, org_id: str, ref: str) -> CredentialRecord:
        """Metadata only - never the secret. For an operator screen."""
        row = self._row(org_id, ref)
        if row is None:
            raise CredentialNotFound(f"no credential for ref {ref!r}")
        return _record(row)

    def list_refs(self, *, org_id: str) -> list[CredentialRecord]:
        """Every credential this organisation has, WITHOUT any payload."""
        rows = self.db.execute(
            select(models.CredentialSecret).where(models.CredentialSecret.org_id == org_id)
        ).scalars().all()
        return [_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _row(self, org_id: str, ref: str) -> Optional[models.CredentialSecret]:
        # Scoped by org_id as well as ref. A caller that guessed another organisation's ref is filtered
        # to its own rows here AND by RLS - the two are independent, which is the point.
        return self.db.execute(
            select(models.CredentialSecret).where(
                models.CredentialSecret.org_id == org_id,
                models.CredentialSecret.ref == ref,
            )
        ).scalars().first()

    def _encrypt(self, payload: dict[str, Any]) -> str:
        try:
            return self._fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            # The message deliberately excludes the payload: an exception string ends up in logs.
            raise CredentialStoreError("could not encrypt the credential") from exc

    def _decrypt(self, ciphertext: str) -> dict[str, Any]:
        from cryptography.fernet import InvalidToken

        try:
            raw = self._fernet.decrypt(ciphertext.encode("ascii"))
        except InvalidToken as exc:
            raise CredentialUndecryptable(
                "the credential could not be decrypted. Either the encryption key has changed, or the "
                "row was altered. Every credential under the previous key is now unreadable."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CredentialUndecryptable("the stored credential is malformed") from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CredentialUndecryptable("the decrypted credential was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise CredentialUndecryptable("the decrypted credential was not a mapping")
        return decoded

    def __repr__(self) -> str:  # noqa: D105
        # Explicit, because the default repr of an object holding a key is a habit, and a habit is how
        # a key reaches a log line.
        return f"<CredentialStore key=<redacted> db={type(self.db).__name__}>"


def store_from_settings(db: Session, settings: Any) -> CredentialStore:
    """Build a store from application settings. Raises when no key is configured.

    Raises rather than returning None: unlike a mail transport, an unconfigured CREDENTIAL STORE is not
    a state the caller can work around by parking the work. Anything that needs a secret needs the
    store, and a None would move the failure to a less obvious place.
    """
    return CredentialStore(db, key=getattr(settings, ENCRYPTION_KEY_SETTING, "") or "")


def generate_key() -> str:
    """A fresh Fernet key. Offered here so an operator does not invent their own, badly."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _record(row: models.CredentialSecret) -> CredentialRecord:
    return CredentialRecord(
        ref=row.ref,
        kind=row.kind,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        rotated_at=row.rotated_at,
        note=row.note,
    )


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())
