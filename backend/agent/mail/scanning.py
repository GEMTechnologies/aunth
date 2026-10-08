"""Attachment malware screening, and an honest account of what it is not.

What this is
------------
A **deterministic, offline screen**: file-type verification against magic bytes,
executable and macro detection by content rather than by extension, archive and
embedded-object detection, oversize and nesting limits, and signature matching for the
standard anti-malware test file.

What this is not
----------------
**It is not an anti-virus engine, and the code says so in the words the customer will
read.** There is no virus database, no heuristic engine, no sandbox and no
detonation. A determined attacker with a novel payload passes this the same way they
pass any signature check.

That honesty is the point. The brief requires the limitation be preserved rather than
papered over, and the failure mode of pretending otherwise is specific: an operator
reads `scan_status = CLEAN` and concludes the file was checked against something that
knows about malware. So the result carries a `coverage` field naming exactly what was
verified, and `CLEAN` here means "passed a type and structure check", never "known
safe".

Why extension-based checks are not enough
----------------------------------------
A file called `accounts.pdf` whose first bytes are `MZ` is a Windows executable. The
extension is attacker-controlled; the magic bytes are not. Every type decision here is
made from content, and the extension is used only to detect *disagreement* — which is
itself a signal worth quarantining.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: How much coverage this scanner has. Named and reported, never implied.
COVERAGE = (
    "NOT an anti-virus engine and NOT a virus database. This is type verification "
    "against file signatures, executable and macro detection, archive and "
    "embedded-object inspection, and known test-file signatures. A novel payload is "
    "not detected."
)


class ScanVerdict(str, Enum):
    CLEAN = "CLEAN"
    SUSPICIOUS = "SUSPICIOUS"
    MALICIOUS = "MALICIOUS"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def rank(self) -> int:
        """How severe a verdict is, so verdicts are comparable."""
        return {
            ScanVerdict.UNAVAILABLE: 0,
            ScanVerdict.CLEAN: 0,
            ScanVerdict.SUSPICIOUS: 1,
            ScanVerdict.MALICIOUS: 2,
        }[self]

    def escalate_to(self, other: "ScanVerdict") -> "ScanVerdict":
        """The more severe of two verdicts.

        Every finding goes through this rather than assigning directly. The first
        version assigned, and a LATER, WEAKER check overwrote an earlier stronger one:
        an ``MZ`` executable named ``.pdf`` was correctly marked MALICIOUS by the
        content check and then downgraded to SUSPICIOUS by the unrecognised-content
        check that ran after it. The file was still quarantined, but the RECORD said
        "suspicious" about a Windows executable - and a verdict that can go down is a
        verdict nobody can rely on.
        """
        return other if other.rank > self.rank else self


#: Magic-byte signatures. Content decides the type; the filename only has to agree.
_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("application/pdf", b"%PDF-"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
    ("application/zip", b"PK\x03\x04"),
    ("application/x-ole-storage", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
)

#: Executable formats. `MZ` is DOS/PE; ELF and Mach-O cover the other platforms, and
#: a script shebang is included because a shell script attached to a grant
#: application has no legitimate reading.
_EXECUTABLE_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("DOS/PE executable", b"MZ"),
    ("ELF executable", b"\x7fELF"),
    ("Mach-O executable", b"\xfe\xed\xfa\xce"),
    ("Mach-O executable", b"\xcf\xfa\xed\xfe"),
    ("Java class", b"\xca\xfe\xba\xbe"),
    ("shell script", b"#!/"),
)

_EXECUTABLE_EXTENSIONS = frozenset({
    ".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".vbs", ".vbe",
    ".js", ".jse", ".jar", ".ps1", ".psm1", ".hta", ".cpl", ".dll", ".lnk",
    ".iso", ".img", ".reg", ".wsf", ".wsh", ".apk", ".dmg", ".app", ".sh",
})
_MACRO_EXTENSIONS = frozenset({".docm", ".xlsm", ".pptm", ".dotm", ".xlam", ".xlsb"})
_ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"})

#: OLE documents that contain a macro stream. `VBA` in a `.doc` is a macro; in a
#: `.docx` the same bytes are a zip entry, which is why both are inspected.
_OLE_MACRO_MARKERS = (b"VBA", b"_VBA_PROJECT", b"Macros")

#: The EICAR test string. Not malware - a published marker that every scanner is
#: expected to detect, which makes it the one signature whose presence proves the
#: scanner runs at all.
# The standard EICAR anti-malware test string. Matched on its distinctive MARKER
# rather than on the full published string: the first version escaped a backslash
# twice, so the one signature whose presence proves the scanner is running matched
# nothing at all. A truncated copy is still the published test file.
_EICAR_MARKER = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"

#: Default ceilings. Generous for a document, and far below what would exhaust memory.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED = 200 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 500

#: Patterns inside text content that indicate an active payload rather than prose.
_SCRIPT_PATTERNS = (
    re.compile(rb"<script[\s>]", re.I),
    re.compile(rb"javascript:", re.I),
    re.compile(rb"ActiveXObject", re.I),
    re.compile(rb"WScript\.Shell", re.I),
    re.compile(rb"ShellExecute", re.I),
    re.compile(rb"powershell\s+-(enc|e|encodedcommand)", re.I),
    re.compile(rb"AutoOpen\s*\(", re.I),
    re.compile(rb"Document_Open\s*\(", re.I),
    re.compile(rb"Workbook_Open\s*\(", re.I),
    re.compile(rb"/Launch\s*/S", re.I),
    re.compile(rb"/JavaScript\s", re.I),
)


@dataclass
class ScanFinding:
    code: str
    detail: str
    severity: str  # "info" | "suspicious" | "malicious"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass
class ScanResult:
    verdict: ScanVerdict
    findings: list[ScanFinding] = field(default_factory=list)
    detected_type: Optional[str] = None
    size_bytes: int = 0
    checksum_sha256: Optional[str] = None
    #: Exactly what was checked. Present on every result, including CLEAN ones, so
    #: "clean" can never be read as "known safe".
    coverage: str = COVERAGE
    is_scanner: bool = False

    @property
    def safe_to_attach_outbound(self) -> bool:
        """Whether an outbound send may carry this.

        Only CLEAN, and only from a real content scan. A result that says UNAVAILABLE
        has not been checked, and attaching an unchecked file is the thing this
        function exists to prevent.
        """
        return self.verdict == ScanVerdict.CLEAN

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "findings": [f.as_dict() for f in self.findings],
            "detected_type": self.detected_type,
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "coverage": self.coverage,
            "note": (
                "This is a structural and signature screen, NOT an anti-virus engine. "
                "CLEAN means the file passed a type and structure check; it does not "
                "mean the file is known to be safe."
            ),
        }


def _extension(filename: Optional[str]) -> str:
    name = (filename or "").lower()
    return name[name.rfind(".") :] if "." in name else ""


def _detect_type(content: bytes) -> Optional[str]:
    for mime, signature in _SIGNATURES:
        if content.startswith(signature):
            return mime
    return None


def _expected_mime_for(extension: str) -> Optional[str]:
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".zip": "application/zip",
        ".doc": "application/x-ole-storage",
        ".xls": "application/x-ole-storage",
        ".ppt": "application/x-ole-storage",
    }.get(extension)


def scan_attachment(
    *,
    content: Optional[bytes],
    filename: Optional[str] = None,
    declared_mime: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_uncompressed: int = DEFAULT_MAX_UNCOMPRESSED,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> ScanResult:
    """Screen one attachment.

    Deterministic, offline, and no network access - so it runs on every inbound file
    without becoming a dependency that can be down.
    """
    if content is None:
        return ScanResult(
            verdict=ScanVerdict.UNAVAILABLE,
            findings=[ScanFinding(
                "NO_CONTENT", "no bytes were supplied, so nothing could be checked", "info"
            )],
            coverage=COVERAGE,
        )

    size = len(content)
    result = ScanResult(
        verdict=ScanVerdict.CLEAN,
        size_bytes=size,
        checksum_sha256=hashlib.sha256(content).hexdigest(),
    )
    findings = result.findings

    # -- 1. size, before anything expensive -------------------------------
    if size == 0:
        findings.append(ScanFinding("EMPTY_FILE", "the file has no content", "suspicious"))
    if size > max_bytes:
        findings.append(ScanFinding(
            "OVERSIZE", f"{size} bytes exceeds the {max_bytes} byte limit", "suspicious"
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)
        return result

    # -- 2. the test signature, which proves the scanner runs ---------------
    if _EICAR_MARKER in content:
        findings.append(ScanFinding(
            "EICAR_TEST_SIGNATURE",
            "the standard anti-malware test string is present",
            "malicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.MALICIOUS)
        return result

    extension = _extension(filename)

    # -- 3. executables, by CONTENT ---------------------------------------
    for label, signature in _EXECUTABLE_SIGNATURES:
        if content.startswith(signature):
            findings.append(ScanFinding(
                "EXECUTABLE_CONTENT",
                f"the content is a {label}, whatever the filename says",
                "malicious",
            ))
            result.verdict = result.verdict.escalate_to(ScanVerdict.MALICIOUS)
            break

    # The extension is attacker-controlled, so it is used only to detect disagreement.
    if extension in _EXECUTABLE_EXTENSIONS and result.verdict != ScanVerdict.MALICIOUS:
        findings.append(ScanFinding(
            "EXECUTABLE_EXTENSION",
            f"the filename claims an executable type ({extension})",
            "malicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.MALICIOUS)

    # -- 4. type agreement -------------------------------------------------
    detected = _detect_type(content)
    result.detected_type = detected
    expected = _expected_mime_for(extension)
    if detected and expected and detected != expected:
        findings.append(ScanFinding(
            "TYPE_MISMATCH",
            f"the filename says {extension} ({expected}) but the content is {detected}",
            "suspicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)
    if detected is None and extension in (".pdf", ".png", ".jpg", ".jpeg", ".zip"):
        # A document extension with no recognisable content is either corrupt or
        # disguised, and neither belongs on an outbound attachment.
        findings.append(ScanFinding(
            "UNRECOGNISED_CONTENT",
            f"the filename says {extension} but the content matches no known format",
            "suspicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)

    # -- 5. macro-enabled documents ----------------------------------------
    if extension in _MACRO_EXTENSIONS:
        findings.append(ScanFinding(
            "MACRO_ENABLED_DOCUMENT",
            f"{extension} documents can carry macros and are quarantined by policy",
            "suspicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)
    if detected == "application/x-ole-storage" and any(m in content for m in _OLE_MACRO_MARKERS):
        findings.append(ScanFinding(
            "EMBEDDED_MACRO_STREAM",
            "the document contains a macro stream",
            "suspicious",
        ))
        result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)

    # -- 6. archives, including the zip-bomb case --------------------------
    if detected == "application/zip" or extension in _ARCHIVE_EXTENSIONS:
        _inspect_archive(
            content, findings, max_uncompressed=max_uncompressed, max_entries=max_entries
        )
        if any(f.severity != "info" for f in findings):
            result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)

    # -- 7. active content in anything we can read as text ------------------
    probe = content[:200_000]
    for pattern in _SCRIPT_PATTERNS:
        if pattern.search(probe):
            findings.append(ScanFinding(
                "ACTIVE_CONTENT",
                f"the file contains embedded script content matching {pattern.pattern!r}",
                "suspicious",
            ))
            result.verdict = result.verdict.escalate_to(ScanVerdict.SUSPICIOUS)
            break

    # Detected, and the caller can see exactly what was and was not checked.
    result.is_scanner = True
    return result


def _inspect_archive(
    content: bytes,
    findings: list[ScanFinding],
    *,
    max_uncompressed: int,
    max_entries: int,
) -> None:
    """Look inside an archive without extracting it.

    A zip bomb is the case a size limit alone misses: a 200 KB archive that expands to
    40 GB passes every byte check on the way in. The declared sizes are read from the
    central directory and summed BEFORE anything is decompressed, so the refusal
    happens without allocating the memory the bomb was designed to consume.
    """
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries:
                findings.append(ScanFinding(
                    "ARCHIVE_TOO_MANY_ENTRIES",
                    f"the archive holds {len(entries)} entries (limit {max_entries})",
                    "suspicious",
                ))
                return

            total = sum(entry.file_size for entry in entries)
            if total > max_uncompressed:
                findings.append(ScanFinding(
                    "ARCHIVE_EXPANSION",
                    f"the archive declares {total} uncompressed bytes (limit "
                    f"{max_uncompressed}); refusing before extraction",
                    "suspicious",
                ))
                return

            for entry in entries:
                name = entry.filename or ""
                if ".." in name or name.startswith("/") or ":" in name:
                    # Path traversal: an archive that writes outside its target is not
                    # a document.
                    findings.append(ScanFinding(
                        "ARCHIVE_PATH_TRAVERSAL",
                        f"the archive contains an unsafe path {name!r}",
                        "malicious",
                    ))
                    return
                if _extension(name) in _EXECUTABLE_EXTENSIONS:
                    findings.append(ScanFinding(
                        "ARCHIVE_CONTAINS_EXECUTABLE",
                        f"the archive contains an executable entry {name!r}",
                        "suspicious",
                    ))
                    return
        # A `.zip`-named file that is not a readable zip is corrupt or disguised.
        if result := None:  # pragma: no cover - keeps the branch shape explicit
            pass
    except zipfile.BadZipFile:
        findings.append(ScanFinding(
            "UNREADABLE_ARCHIVE",
            "the file is named as an archive but is not a readable one",
            "suspicious",
        ))


#: `ScanVerdict` -> `mail_attachments.scan_status`.
#:
#: DECLARED rather than an if/elif chain inside a 200-line method, for the reason this whole
#: phase exists: a chain has an `else`, so a NEW verdict would fall silently into
#: `UNAVAILABLE` instead of raising. A dict has no `else`, and `scan_status_for` raises
#: `KeyError` naming the verdict it does not know.
#:
#: Plain strings, not `models.MailAttachment.SCAN_*`. This module is deliberately free of
#: database and network dependencies - it runs on every inbound file, and it must not be
#: something that can be down. The bridge to the model is asserted by a test instead, which
#: also catches a value here that the model does not define.
SCAN_STATUS_BY_VERDICT: dict["ScanVerdict", str] = {
    ScanVerdict.CLEAN: "CLEAN",
    ScanVerdict.SUSPICIOUS: "SUSPICIOUS",
    # A MATCH is not the same record as a hunch. See the note on SCAN_MALICIOUS.
    ScanVerdict.MALICIOUS: "MALICIOUS",
    ScanVerdict.UNAVAILABLE: "UNAVAILABLE",
}


def scan_status_for(verdict: "ScanVerdict", *, has_content: bool) -> str:
    """The `scan_status` for a verdict.

    `has_content` is not decoration. A CLEAN verdict with no content is not a clean file: it
    is a file that was never supplied, and recording it as CLEAN would be the exact
    misreading `CLEAN` must never invite. It becomes UNAVAILABLE.

    Raises `KeyError` for a verdict this bridge does not know, which is the point - the
    previous if/elif chain answered `UNAVAILABLE` for an unknown verdict, so adding one to
    the enum would have silently downgraded every file it matched.
    """
    if verdict is ScanVerdict.CLEAN and not has_content:
        return "UNAVAILABLE"
    return SCAN_STATUS_BY_VERDICT[verdict]


def scan_many(
    attachments: Any, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> dict[str, ScanResult]:
    """Scan a set, keyed by checksum so a duplicate is screened once."""
    results: dict[str, ScanResult] = {}
    for attachment in attachments or ():
        content = (
            attachment.get("content")
            if isinstance(attachment, dict)
            else getattr(attachment, "content", None)
        )
        filename = (
            attachment.get("filename")
            if isinstance(attachment, dict)
            else getattr(attachment, "filename", None)
        )
        mime = (
            attachment.get("mime_type")
            if isinstance(attachment, dict)
            else getattr(attachment, "mime_type", None)
        )
        result = scan_attachment(
            content=content, filename=filename, declared_mime=mime, max_bytes=max_bytes
        )
        key = result.checksum_sha256 or f"unknown-{len(results)}"
        results[key] = result
    return results
