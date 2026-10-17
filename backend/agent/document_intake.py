"""Taking a document from an organisation, and reading it.

WHY THIS EXISTS. The platform could generate documents and could not receive one. There was no upload
route, no storage for uploaded bytes, and therefore no way for a scanned registration certificate - the
single most common thing an NGO actually has - to enter the system at all. `perception.document_from_vision`
was written to read exactly that kind of document and had no caller, because nothing could arrive.

THE ORDER IS DELIBERATE AND FOLLOWS §2C: extraction first, vision when the page has no text layer.
  * an image (a scan, a photograph) has no text layer by definition, so it goes straight to vision;
  * a PDF cannot be read here at all, because this deployment has no PDF library, and saying so is
    better than pretending - the limitation is recorded on the observation rather than swallowed.

A READING IS NOT A FACT. Everything vision produces is VISION-channel, and `perception.FACT_CHANNELS`
excludes VISION, so text read off a scan can never be written into a form as the organisation's own
claim. The organisation said "here is my certificate"; the model said "it appears to say this"; those are
different statements and the type system keeps them apart.

BYTES LIVE ON DISK, NOT IN THE DATABASE. `documents.storage_key` is a reference, the checksum proves the
file is the one referenced, and the store is per-organisation so one tenant's directory cannot be
listed to find another's.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Where uploaded bytes live. Mounted read-write into the API, which is the only writer.
DOCUMENT_STORE = "/var/lib/granada/documents"

#: Formats a vision model can actually be shown. A PDF is not one of them - see the module docstring.
IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

#: Ten megabytes, matching `config.max_upload_size`.
MAX_BYTES = 10 * 1024 * 1024

PROMPT_VERSION = "document-vision/v1"


class IntakeError(RuntimeError):
    """The document could not be accepted or read."""


@dataclass
class IntakeResult:
    document_id: str
    storage_key: str
    checksum: str
    size_bytes: int
    read: bool
    reading: Optional[dict[str, Any]] = None
    limitation: Optional[str] = None


def _safe_stem(filename: str) -> str:
    """A filename that cannot escape the directory or surprise the filesystem."""
    stem = Path(filename or "document").stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
    return (cleaned or "document")[:80]


def _organisation_dir(org_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9-]", "", org_id or "")
    if not safe:
        raise IntakeError("an organisation is required to store a document")
    return Path(DOCUMENT_STORE) / safe


def store_upload(
    *,
    db: Any,
    org_id: str,
    filename: str,
    mime_type: str,
    data: bytes,
    title: Optional[str] = None,
    doc_type: str = "registration_certificate",
    uploaded_by: Optional[str] = None,
) -> IntakeResult:
    """Write the bytes, record the document, and return what was stored.

    The document is written PENDING: uploading is not approving, and the gap between the two is where
    the wrong document would otherwise get attached to a real application.
    """
    from agent.organisation_memory import DocumentVault

    if not data:
        raise IntakeError("the uploaded file is empty")
    if len(data) > MAX_BYTES:
        raise IntakeError(f"the file is {len(data)} bytes; the limit is {MAX_BYTES}")

    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime not in IMAGE_MIME_TYPES and mime != "application/pdf":
        raise IntakeError(
            f"{mime or 'unknown type'} is not accepted; send an image (PNG, JPEG, WebP) or a PDF"
        )

    directory = _organisation_dir(org_id)
    directory.mkdir(parents=True, exist_ok=True)

    checksum = hashlib.sha256(data).hexdigest()
    suffix = IMAGE_MIME_TYPES.get(mime) or ".pdf"
    # CONTENT-ADDRESSED: the same bytes stored twice is the same file, and a re-upload of a changed
    # certificate under the same name gets a different path rather than overwriting the approved one.
    target = directory / f"{_safe_stem(filename)}-{checksum[:16]}{suffix}"
    target.write_bytes(data)

    vault = DocumentVault(db, org_id)
    document = vault.add_version(
        title=(title or Path(filename or "document").name)[:255],
        doc_type=doc_type,
        storage_key=str(target),
        checksum_sha256=checksum,
        mime_type=mime,
        size_bytes=len(data),
        uploaded_by=uploaded_by,
    )
    db.flush()

    logger.info(
        "document_intake.stored",
        extra={"org_id": org_id, "document_id": str(document.id), "mime": mime, "bytes": len(data)},
    )
    return IntakeResult(
        document_id=str(document.id),
        storage_key=str(target),
        checksum=checksum,
        size_bytes=len(data),
        read=False,
    )


def read_document(
    *, db: Any, org_id: str, storage_key: str, mime_type: str, settings: Any = None
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Read the document's text: vision for an image, and an honest limitation for anything else.

    Returns ``(read, reading, limitation)``. A `False` with a limitation is a real answer - "this file
    has no text layer this deployment can read" - and is recorded rather than treated as a failure.
    """
    from agent import perception
    from agent.model_gateway import build_gateway_from_settings
    from agent.multimodal_routing import ImageRef, Observation

    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime not in IMAGE_MIME_TYPES:
        # NO PDF LIBRARY HERE. Declared, not guessed at: an organisation told "we could not read this"
        # can send a photograph instead, whereas a silent empty reading looks like a blank document.
        return (
            False,
            None,
            "this deployment cannot extract text from PDFs; send an image of the document instead",
        )

    path = Path(storage_key)
    if not path.is_file():
        return False, None, "the stored file is missing, so it cannot be read"

    if settings is None:
        from config import settings as settings  # type: ignore[assignment]

    try:
        gateway = build_gateway_from_settings(db, settings)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"no model gateway: {type(exc).__name__}"

    observation = Observation(
        url="",
        title=path.name,
        screenshot=ImageRef(
            ref=str(path), captured_at=datetime.now(timezone.utc), width=0, height=0, label=path.name
        ),
    )
    try:
        values = perception.read_page_with_vision(
            observation,
            gateway=gateway,
            org_id=org_id,
            prompt_version=PROMPT_VERSION,
            image_loader=lambda ref: Path(ref).read_bytes(),
            max_output_tokens=4096,
        )
    except Exception as exc:  # noqa: BLE001 - a reading failure is reported, never raised at a caller
        return False, None, f"{type(exc).__name__}: {exc}"[:200]

    document_reading = perception.document_from_vision(
        document_id=path.stem,
        filename=path.name,
        mime=mime,
        readings=values,
        screenshot_ref=str(path),
    )

    reading = {
        "prompt_version": PROMPT_VERSION,
        "method": document_reading.method,
        "needed_vision": document_reading.needed_vision,
        # VISION-channel only. A caller that writes these into a form is the failure this field is
        # shaped to prevent.
        "values": [
            {"name": v.name, "value": v.value, "channel": getattr(v.channel, "value", v.channel)}
            for v in document_reading.values
        ],
        "limitation": document_reading.limitation,
    }
    return True, reading, document_reading.limitation
