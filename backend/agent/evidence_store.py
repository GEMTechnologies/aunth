"""Where a browser run's evidence lives, separately from the scratch it was produced in.

THE DEFECT THIS FIXES
---------------------
A run captured 16,982-byte PNGs with valid magic bytes, referenced them in its outcome, and left ZERO
files on disk. The screenshots were written into the per-tenant Chromium profile directory, and
`close()` deletes that directory - correctly, because it holds cookies and session state that must not
outlive the run.

So the scratch cleanup took the evidence with it. Every `evidence` reference in every report pointed at
a file that no longer existed, which is worse than having none: **it looks like evidence.**

THE SEPARATION
--------------
Two lifetimes, two places:

  * the PROFILE is scratch. It holds cookies, local storage and session state for one organisation.
    It must be destroyed at the end of the run, and destroying it is a security property, not
    housekeeping.
  * the EVIDENCE is a deliberate artefact. It is small, it is referenced in an outcome a human may
    read weeks later, and it needs its own retention and access control.

They were in the same directory. Now they are not.

WHAT IS ENFORCED HERE
---------------------
Tenant separation in the PATH, so two organisations cannot collide or read each other's evidence even
if a filename repeats. Filename sanitisation, because a name derived from a page or a donor is
attacker-influenced and §10 requires path traversal rejection. Bounded count and size, because a page
that keeps producing screenshots is the same unbounded-work problem the runtime already bounds for
actions.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

#: Default root. Deliberately NOT under the Chromium profile directory and not under /tmp's
#: per-run scratch: evidence must survive the run that produced it.
DEFAULT_EVIDENCE_ROOT = "/var/lib/granada/browser-evidence"

#: A filename may not escape its directory. Anything outside this is replaced rather than escaped.
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

MAX_FILES_PER_RUN = 40
MAX_TOTAL_BYTES_PER_RUN = 32 * 1024 * 1024


class EvidenceError(RuntimeError):
    """Evidence could not be stored. Raised rather than silently dropped, because an outcome that
    references evidence must not be produced when the evidence was not written."""


@dataclass(frozen=True)
class StoredEvidence:
    """A reference that still resolves after the run has ended."""

    ref: str
    kind: str
    bytes_written: int
    stored_at: datetime
    #: SHA-256 of the bytes, so a later reader can show the file is the one referenced rather than
    #: one that happens to share a name. Same reason SubmissionPackage carries checksums.
    checksum: str = ""


@dataclass
class EvidenceStore:
    """One store per run. Not a global singleton: the run's lifetime IS the collection's lifetime,
    and a shared store would make cross-tenant mixing possible through a stale reference.
    """

    root: Path
    org_id: str
    run_id: str
    max_files: int = MAX_FILES_PER_RUN
    max_total_bytes: int = MAX_TOTAL_BYTES_PER_RUN
    _written: list[StoredEvidence] = field(default_factory=list)
    _total: int = 0

    def directory(self) -> Path:
        """TENANT-SCOPED, and created under the store's own root rather than a shared temp dir.

        The organisation and the run both appear in the path. Two organisations cannot collide even
        when a filename repeats, and a directory listing of one run cannot show another's.
        """
        return self.root / _segment(self.org_id) / _segment(self.run_id)

    def store(self, *, kind: str, source: Path, name: str) -> StoredEvidence:
        """Copy a produced artefact into the evidence area and return a durable reference.

        A COPY, not a move: the source still belongs to the browser's scratch area, and the run may
        still need it. The reference handed out is the copy, which is the one that survives.
        """
        if len(self._written) >= self.max_files:
            raise EvidenceError(
                f"this run has already stored {len(self._written)} artefacts; the bound is "
                f"{self.max_files}, which exists because a page that keeps producing screenshots is "
                "the same unbounded-work problem the runtime bounds for actions"
            )

        if not source.exists():
            # Named explicitly. A reference to a file that was never written is the defect this whole
            # module exists to prevent, and silently returning one would recreate it.
            raise EvidenceError(f"cannot store {kind}: {source} does not exist")

        size = source.stat().st_size
        if self._total + size > self.max_total_bytes:
            raise EvidenceError(
                f"storing {size} more bytes would exceed the {self.max_total_bytes}-byte bound for "
                "this run"
            )

        target_dir = self.directory()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _safe_name(name, kind=kind)

        data = source.read_bytes()
        target.write_bytes(data)

        record = StoredEvidence(
            ref=str(target),
            kind=kind,
            bytes_written=len(data),
            stored_at=datetime.now(timezone.utc),
            checksum=hashlib.sha256(data).hexdigest(),
        )
        self._written.append(record)
        self._total += len(data)
        return record

    @property
    def stored(self) -> list[StoredEvidence]:
        return list(self._written)

    def total_bytes(self) -> int:
        return self._total

    def verify(self) -> list[str]:
        """Which references no longer resolve. Should always be empty; returned rather than asserted
        so a caller can report a broken reference instead of discovering it weeks later from a
        report."""
        missing: list[str] = []
        for record in self._written:
            path = Path(record.ref)
            if not path.exists():
                missing.append(record.ref)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != record.checksum:
                missing.append(f"{record.ref} (checksum mismatch)")
        return missing


def _segment(value: str) -> str:
    """One path component, incapable of traversal.

    `..` is removed by the character filter (dots are allowed for extensions, so `..` is handled
    explicitly), and a value that reduces to nothing becomes a fixed placeholder rather than an empty
    component that would silently collapse the path.
    """
    cleaned = _SAFE.sub("_", (value or "").strip())
    cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip("._")
    return cleaned[:128] or "unknown"


def _safe_name(name: str, *, kind: str) -> str:
    """A filename that cannot escape its directory and cannot be empty.

    A name may be derived from a page title or a donor's document name, both of which are
    attacker-influenced. It is REPLACED rather than escaped, because escaping is where people get
    traversal wrong.
    """
    base = os.path.basename(name or "")
    cleaned = _SAFE.sub("_", base).replace("..", "_").strip("._")
    if not cleaned:
        cleaned = f"{_segment(kind) or 'evidence'}.bin"
    return cleaned[:180]


def open_store(*, org_id: str, run_id: str, root: Optional[str] = None, **bounds: Any) -> EvidenceStore:
    """Open the evidence area for one run."""
    return EvidenceStore(
        root=Path(root or os.environ.get("GRANADA_EVIDENCE_ROOT") or DEFAULT_EVIDENCE_ROOT),
        org_id=org_id,
        run_id=run_id,
        **bounds,
    )


def describe() -> dict[str, Any]:
    """The rules, stated where a reviewer will find them."""
    return {
        "default_root": DEFAULT_EVIDENCE_ROOT,
        "separate_from": (
            "the Chromium profile directory, which holds cookies and session state and is destroyed "
            "at the end of every run - correctly, and that destruction used to take the evidence "
            "with it"
        ),
        "tenant_scoped_path": "root/<org>/<run>/ - two organisations cannot collide on a filename",
        "traversal": "filenames are replaced, not escaped; path segments cannot contain '..'",
        "bounded": {
            "max_files_per_run": MAX_FILES_PER_RUN,
            "max_total_bytes_per_run": MAX_TOTAL_BYTES_PER_RUN,
        },
        "integrity": "every record carries a SHA-256, and verify() reports any that no longer match",
        "does_not_do": [
            "it does not delete evidence; retention is a separate decision",
            "it does not store credentials or page text - artefacts only",
        ],
    }
