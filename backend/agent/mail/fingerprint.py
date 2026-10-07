"""The approval fingerprint: what a human actually authorises.

The invariant, stated once and enforced everywhere else:

    A human approves ONE fingerprint. If ANY material field changes, the approval
    is invalid. There is no approval inheritance.

Why a fingerprint rather than a reference
-----------------------------------------
An approval that said "send draft 12" would be an approval of a *pointer*. Edit the
draft, and the pointer still resolves - to different words. The human authorised
text they read; the system would send text they never saw. That is the failure this
module exists to make impossible.

So the fingerprint is over a **canonical serialisation of the content itself**:
envelope, subject, the exact body bytes, and an attachment manifest carrying each
document's id, version, filename, mime type and checksum. Not ids alone - a version
bump or a re-upload changes the checksum, and the checksum is what proves the bytes
are the ones that were approved.

Canonicalisation rules, and why each one matters
-----------------------------------------------
**Ordering is normalised.** ``to``/``cc``/``bcc`` are sorted and lowercased, so
re-ordering recipients does not produce a different fingerprint for an identical
message. Without this, an unrelated code change that reordered a list would
invalidate every pending approval - which trains people to re-approve without
reading, defeating the whole mechanism.

**Absent and empty are the same.** ``None``, ``[]`` and ``""`` all canonicalise to
an empty marker, so "no CC" is one value rather than three that hash differently.

**Whitespace in the body is preserved exactly.** A trailing newline is a change a
human might notice and a hash must. Line endings are normalised to ``\\n`` because
the same message assembled on Windows and Linux is the same message, and treating
it as different would refuse valid approvals.

**Unicode is NFC-normalised.** The same visible text can be encoded two ways;
without normalisation, a body that renders identically would fail its own approval.

**The version is included.** A material field added later cannot be silently absent
from an old fingerprint - the version makes the schema change explicit and forces a
re-approval rather than leaving a gap.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

#: Bump when the canonical field set changes. An old approval must not survive a
#: change to what "the exact message" means.
FINGERPRINT_VERSION = "granada-mail-v1"

#: Separators chosen so a field's content cannot forge a boundary. A body
#: containing the delimiter would otherwise be able to impersonate the field after
#: it. ``\x1f`` is the ASCII unit separator: it cannot appear in a legitimatemail
#: header, and its presence in a body is itself evidence of tampering.
UNIT = "\x1f"
RECORD = "\x1e"
#: Field separator between key and value.
KEYSEP = "\x1d"


def _normalise_text(value: Optional[str]) -> str:
    """Unicode NFC, line endings unified, trailing whitespace preserved.

    Deliberately does NOT strip. Stripping would make "Dear Sir " and "Dear Sir"
    the same message, and a trailing space is a change a hash must catch - the
    point of the fingerprint is to be stricter than a human reader, not looser.
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def _normalise_addresses(values: Any) -> list[str]:
    """Sorted, lowercased, deduplicated, trimmed.

    Sorted so re-ordering is not a change; lowercased because the local part is
    formally case-sensitive but every real provider treats it as insensitive, and
    hashing case-sensitively would refuse a valid re-approval for a capitalised
    duplicate. Deduplicated because sending to the same address twice is the same
    message.
    """
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    cleaned = {
        unicodedata.normalize("NFC", str(value)).strip().lower()
        for value in values
        if str(value or "").strip()
    }
    return sorted(cleaned)


@dataclass
class AttachmentManifestEntry:
    """One attachment, frozen at the moment of approval."""

    document_id: str
    version: Optional[int]
    storage_ref: Optional[str]
    filename: Optional[str]
    mime_type: Optional[str]
    checksum_sha256: Optional[str]

    def canonical(self) -> str:
        return KEYSEP.join([
            "attachment",
            _normalise_text(self.document_id),
            "" if self.version is None else str(self.version),
            _normalise_text(self.storage_ref),
            _normalise_text(self.filename),
            _normalise_text(self.mime_type),
            _normalise_text(self.checksum_sha256),
        ])

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "version": self.version,
            "storage_ref": self.storage_ref,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "checksum_sha256": self.checksum_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttachmentManifestEntry":
        return cls(
            document_id=str(data.get("document_id") or ""),
            version=data.get("version"),
            storage_ref=data.get("storage_ref"),
            filename=data.get("filename"),
            mime_type=data.get("mime_type"),
            checksum_sha256=data.get("checksum_sha256"),
        )


@dataclass
class FingerprintInput:
    """Everything a human is authorising, in canonical form."""

    org_id: str
    agent_id: str

    from_address: Optional[str]
    to_addresses: Any
    cc_addresses: Any = None
    bcc_addresses: Any = None
    reply_to_address: Optional[str] = None

    subject: Optional[str] = None
    body: Optional[str] = None
    attachments: tuple[AttachmentManifestEntry, ...] = ()

    application_id: Optional[str] = None
    thread_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None

    draft_version: Optional[int] = None
    risk_class: Optional[str] = None

    version: str = FINGERPRINT_VERSION

    def canonical(self) -> str:
        """The exact string that gets hashed.

        Stored alongside the fingerprint so a mismatch is *diagnosable* rather than
        merely detected: an operator can diff the approved canonical form against
        the current one and see which field moved, instead of being told only that
        a hash differs.
        """
        parts = [
            KEYSEP.join(["version", self.version]),
            KEYSEP.join(["org_id", _normalise_text(self.org_id)]),
            KEYSEP.join(["agent_id", _normalise_text(self.agent_id)]),
            KEYSEP.join(["from", _normalise_text(self.from_address).strip().lower()]),
            KEYSEP.join(["to", * _normalise_addresses(self.to_addresses)]),
            KEYSEP.join(["cc", *_normalise_addresses(self.cc_addresses)]),
            KEYSEP.join(["bcc", *_normalise_addresses(self.bcc_addresses)]),
            KEYSEP.join(["reply_to", _normalise_text(self.reply_to_address).strip().lower()]),
            KEYSEP.join(["subject", _normalise_text(self.subject)]),
            # The body is the LAST field before attachments and is length-prefixed,
            # so a body containing the record separator cannot impersonate a
            # following field. Without the length prefix, a body ending in
            # "\x1eattachment\x1d..." would forge an attachment entry.
            KEYSEP.join(["body_len", str(len(_normalise_text(self.body)))]),
            _normalise_text(self.body),
            KEYSEP.join(["application_id", _normalise_text(self.application_id)]),
            KEYSEP.join(["thread_id", _normalise_text(self.thread_id)]),
            KEYSEP.join(["reply_to_message_id", _normalise_text(self.reply_to_message_id)]),
            KEYSEP.join(["draft_version", "" if self.draft_version is None else str(self.draft_version)]),
            KEYSEP.join(["risk_class", _normalise_text(self.risk_class)]),
            KEYSEP.join(["attachment_count", str(len(self.attachments))]),
            # Sorted by document id so a re-ordered manifest is not a change, while
            # a changed checksum absolutely is.
            *sorted(entry.canonical() for entry in self.attachments),
        ]
        return RECORD.join(parts)

    def fingerprint(self) -> str:
        """SHA-256 over the canonical form, hex-encoded."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def fingerprint(
    *,
    org_id: str,
    agent_id: str,
    from_address: Optional[str],
    to_addresses: Any,
    cc_addresses: Any = None,
    bcc_addresses: Any = None,
    reply_to_address: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    attachments: Iterable[Any] = (),
    application_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    draft_version: Optional[int] = None,
    risk_class: Optional[str] = None,
) -> tuple[str, str]:
    """Calculate a fingerprint. Returns ``(fingerprint, canonical_input)``.

    Returning the canonical input as well as the digest is deliberate: the caller
    stores both, so a later mismatch can be explained rather than only refused.
    """
    entries: list[AttachmentManifestEntry] = []
    for item in attachments or ():
        if isinstance(item, AttachmentManifestEntry):
            entries.append(item)
        elif isinstance(item, dict):
            entries.append(AttachmentManifestEntry.from_dict(item))
        else:
            entries.append(
                AttachmentManifestEntry(
                    document_id=str(getattr(item, "id", "") or ""),
                    version=getattr(item, "version", None),
                    storage_ref=getattr(item, "storage_ref", None) or getattr(item, "storage_key", None),
                    filename=getattr(item, "filename", None),
                    mime_type=getattr(item, "mime_type", None),
                    checksum_sha256=getattr(item, "checksum_sha256", None),
                )
            )

    material = FingerprintInput(
        org_id=org_id,
        agent_id=agent_id,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        bcc_addresses=bcc_addresses,
        reply_to_address=reply_to_address,
        subject=subject,
        body=body,
        attachments=tuple(entries),
        application_id=application_id,
        thread_id=thread_id,
        reply_to_message_id=reply_to_message_id,
        draft_version=draft_version,
        risk_class=risk_class,
    )
    return material.fingerprint(), material.canonical()


def diff_fingerprint_inputs(approved: Optional[str], current: Optional[str]) -> dict[str, Any]:
    """Which canonical fields differ between an approved and a current message.

    Exists so a refusal is *useful*. "The approval does not match" tells an operator
    nothing; "the recipient changed" tells them what to do next. The comparison is
    line-wise over the canonical form, which is stable because the field order is
    fixed.
    """
    if not approved or not current:
        return {"comparable": False, "reason": "one side of the comparison is missing"}

    approved_lines = approved.split(RECORD)
    current_lines = current.split(RECORD)
    changed: list[dict[str, str]] = []
    for index in range(max(len(approved_lines), len(current_lines))):
        left = approved_lines[index] if index < len(approved_lines) else ""
        right = current_lines[index] if index < len(current_lines) else ""
        if left == right:
            continue
        field_name = left.split(KEYSEP)[0] or right.split(KEYSEP)[0] or f"field_{index}"
        # Values are shown truncated and never include the body: this output goes
        # into an error message, and an error message must not carry correspondence.
        changed.append({
            "field": field_name,
            "approved": left[:200] if field_name != "body" else "<body differs>",
            "current": right[:200] if field_name != "body" else "<body differs>",
        })
    return {"comparable": True, "changed": changed}
