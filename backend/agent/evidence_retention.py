"""Retention for browser evidence: what to keep, what to thin, and what to let go.

THE DECISION THAT WAS NEVER MADE. `evidence_store` bounds a SINGLE run (40 files, 32 MB) and its
`describe()` says plainly that "it does not delete evidence; retention is a separate decision". It
was never made, so evidence accumulated forever: measured on production, 237 MB across 38 run
directories, growing ~6 MB per registration attempt per organisation. At a thousand organisations
that is gigabytes a day.

THE HARD PROBLEM IS NOT DISK, IT IS TRUST. A removed file breaks a reference that a report may point
at. The module this extends exists precisely because "evidence reference that resolves to nothing"
is worse than no evidence at all - it looks like evidence. So retention is built in three parts:

  1. **PLAN BEFORE DELETE.** `plan_prune` is pure: it reads the tree and returns exactly what would
     be removed, and nothing on disk changes. `apply_prune` is the only thing that deletes, and it
     re-checks every path first. A scheduled job can therefore run in dry-run mode indefinitely.
  2. **THIN BEFORE REMOVING.** For a while, a run keeps a representative sample rather than
     disappearing: the first and last frames are the ones an audit actually reads (what the page
     looked like when the run started and how it ended). Intermediate frames go first.
  3. **SAY WHAT WAS REMOVED.** The result reports the files and bytes removed, so a dangling
     reference is traceable to a decision rather than to a bug.

WHAT IS DELIBERATELY NOT AUTOMATIC. Nothing in the fleet loop calls `apply_prune`. Deleting a
customer's audit trail is not a thing to do behind their back on a timer; it is an operator action
with a plan they can read first. The scheduling that DOES happen is the dry run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agent.evidence_store import DEFAULT_EVIDENCE_ROOT

logger = logging.getLogger("evidence_retention")

#: Keep every frame for a month. Long enough to investigate a live complaint, short enough that the
#: bulk of the storage is bounded.
DEFAULT_KEEP_FULL_DAYS = 30

#: After that, keep a sample for three months - the window in which somebody is still likely to ask
#: "what did the portal show you?" - then let the run go.
DEFAULT_KEEP_SAMPLED_DAYS = 90

#: How many frames survive down-sampling: the first and the last are what an audit reads.
DEFAULT_SAMPLE_FRAMES = 4

#: Never walk forever. A directory with more entries than this is a defect, not a big customer.
MAX_RUNS_SCANNED = 5000


class RetentionError(RuntimeError):
    """A plan or an apply that would do something unsafe."""


@dataclass(frozen=True)
class RetentionPolicy:
    """What to keep. Days are measured from a file's modification time."""

    keep_full_days: int = DEFAULT_KEEP_FULL_DAYS
    keep_sampled_days: int = DEFAULT_KEEP_SAMPLED_DAYS
    sample_frames: int = DEFAULT_SAMPLE_FRAMES
    #: Optional hard cap across the whole store. When exceeded, the oldest runs are released even if
    #: they are inside the sampled window - disk exhaustion is a worse outcome than a short history.
    max_total_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        if self.keep_full_days < 0 or self.keep_sampled_days < 0:
            raise RetentionError("retention windows cannot be negative")
        if self.keep_sampled_days < self.keep_full_days:
            raise RetentionError(
                "keep_sampled_days must be at least keep_full_days: a sampled window shorter than "
                "the full window would delete files that the full window was meant to protect"
            )
        if self.sample_frames < 2:
            raise RetentionError(
                "sample_frames must be at least 2: the point of the sample is to keep the first and "
                "last frames, and one frame is neither"
            )


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int
    mtime: datetime


@dataclass
class RunEntry:
    directory: Path
    org: str
    run_id: str
    files: list[FileEntry] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def newest(self) -> Optional[datetime]:
        return max((f.mtime for f in self.files), default=None)


@dataclass
class PrunePlan:
    """What a prune would do. Empty `delete` means nothing to do, not that nothing was checked."""

    delete: list[FileEntry] = field(default_factory=list)
    keep: list[FileEntry] = field(default_factory=list)
    runs_scanned: int = 0
    runs_downsampled: int = 0
    runs_released: int = 0
    runs_over_cap: int = 0

    @property
    def bytes_freed(self) -> int:
        return sum(f.size for f in self.delete)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs_scanned": self.runs_scanned,
            "runs_downsampled": self.runs_downsampled,
            "runs_released": self.runs_released,
            "runs_over_cap": self.runs_over_cap,
            "files_to_delete": len(self.delete),
            "files_to_keep": len(self.keep),
            "bytes_freed": self.bytes_freed,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scan(root: Path) -> list[RunEntry]:
    """Every run directory under the root, as `root/<org>/<run>/*`.

    The depth is fixed at two because that is what `EvidenceStore.directory()` produces. Walking
    arbitrarily deep would let a stray copy of somebody's filesystem be treated as evidence - and
    then pruned.
    """
    if not root.is_dir():
        return []

    runs: list[RunEntry] = []
    for org_dir in sorted(root.iterdir()):
        if not org_dir.is_dir() or org_dir.is_symlink():
            continue
        for run_dir in sorted(org_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            files: list[FileEntry] = []
            for item in sorted(run_dir.iterdir()):
                # SyMLINKS ARE SKIPPED, not followed. Following one would let a prune reach outside
                # the evidence root, which is the one thing this must never do.
                if item.is_symlink() or not item.is_file():
                    continue
                stat = item.stat()
                files.append(
                    FileEntry(
                        path=item,
                        size=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                )
            runs.append(RunEntry(directory=run_dir, org=org_dir.name, run_id=run_dir.name, files=files))
            if len(runs) >= MAX_RUNS_SCANNED:
                logger.warning("evidence_retention_scan_bounded", extra={"limit": MAX_RUNS_SCANNED})
                return runs
    return runs


def _sample(files: list[FileEntry], count: int) -> list[FileEntry]:
    """A representative subset: the first and last, then evenly spaced between.

    Returns them in the ORIGINAL order, because "frames 1 and N" is only meaningful if the numbering
    is the run's own. `files` is sorted by name, and `evidence_store` writes `shot-<ordinal>.png`, so
    name order is frame order.
    """
    if len(files) <= count:
        return list(files)
    if count == 2:
        return [files[0], files[-1]]
    step = (len(files) - 1) / (count - 1)
    chosen = {round(i * step) for i in range(count)}
    return [f for index, f in enumerate(files) if index in chosen]


def plan_prune(
    root: Path | str | None = None,
    policy: RetentionPolicy | None = None,
    *,
    now: Optional[datetime] = None,
) -> PrunePlan:
    """Work out what retention would remove. READ-ONLY: nothing on disk changes.

    Being pure is the point. An operator (or a test) can inspect the answer, and a scheduled job can
    run this every night forever without ever deleting anything.
    """
    policy = policy or RetentionPolicy()
    root_path = Path(root or os.environ.get("GRANADA_EVIDENCE_ROOT") or DEFAULT_EVIDENCE_ROOT)
    moment = now or _now()

    full_cutoff = moment - timedelta(days=policy.keep_full_days)
    sampled_cutoff = moment - timedelta(days=policy.keep_sampled_days)

    plan = PrunePlan()
    runs = _scan(root_path)
    plan.runs_scanned = len(runs)

    for run in runs:
        if not run.files:
            continue
        newest = run.newest or moment

        if newest < sampled_cutoff:
            # Beyond the sampled window: the run goes entirely.
            plan.delete.extend(run.files)
            plan.runs_released += 1
            continue

        if newest < full_cutoff:
            # Inside the sampled window: keep a representative sample, drop the rest.
            keep = _sample(run.files, policy.sample_frames)
            keep_paths = {f.path for f in keep}
            plan.keep.extend(keep)
            plan.delete.extend(f for f in run.files if f.path not in keep_paths)
            plan.runs_downsampled += 1
            continue

        plan.keep.extend(run.files)

    # THE HARD CAP, applied last and only to runs the age rules already agreed could go or be thinned.
    # A cap that could delete a fresh run would make the full-retention window a lie.
    if policy.max_total_bytes is not None:
        kept_bytes = sum(f.size for f in plan.keep)
        over = kept_bytes - policy.max_total_bytes
        if over > 0:
            # Oldest kept runs first.
            candidates = sorted(plan.keep, key=lambda f: f.mtime)
            released: set[Path] = set()
            for entry in candidates:
                if over <= 0:
                    break
                plan.delete.append(entry)
                released.add(entry.path)
                run_dir = entry.path.parent
                # Count a run as "over cap" once, when its first file is released.
                if not any(p.parent == run_dir for p in released if p != entry.path):
                    plan.runs_over_cap += 1
                over -= entry.size
            plan.keep = [f for f in plan.keep if f.path not in released]

    return plan


@dataclass
class PruneResult:
    deleted: int = 0
    bytes_freed: int = 0
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"deleted": self.deleted, "bytes_freed": self.bytes_freed, "failed": self.failed}


def apply_prune(plan: PrunePlan, *, root: Path | str | None = None) -> PruneResult:
    """Delete what the plan says. Refuses any path outside the evidence root.

    THE CONTAINMENT CHECK IS THE IMPORTANT PART. `plan` is a data structure that a caller could
    construct by hand, so `apply` does not trust it: every path is resolved and compared against the
    root before anything is unlinked. A prune that followed a path out of the evidence directory
    would be a data-loss bug of the worst kind.
    """
    root_path = Path(root or os.environ.get("GRANADA_EVIDENCE_ROOT") or DEFAULT_EVIDENCE_ROOT).resolve()
    result = PruneResult()

    for entry in plan.delete:
        try:
            resolved = entry.path.resolve()
        except OSError:
            result.failed.append(f"{entry.path}: unresolvable")
            continue

        if not resolved.is_relative_to(root_path):
            # REFUSED, not skipped silently: a path outside the root is a bug in the caller, and an
            # operator needs to hear about it rather than wonder why bytes were not freed.
            result.failed.append(f"{entry.path}: outside the evidence root, refused")
            logger.error("evidence_prune_outside_root", extra={"path": str(entry.path)[:200]})
            continue

        if not resolved.is_file() or resolved.is_symlink():
            result.failed.append(f"{entry.path}: not a regular file")
            continue

        try:
            size = resolved.stat().st_size
            resolved.unlink()
            result.deleted += 1
            result.bytes_freed += size
        except OSError as exc:
            result.failed.append(f"{entry.path}: {type(exc).__name__}")

    logger.info(
        "evidence_prune_applied",
        extra={"deleted": result.deleted, "bytes_freed": result.bytes_freed,
               "failed": len(result.failed)},
    )
    return result


def describe() -> dict[str, Any]:
    """The policy, stated where a reviewer will find it."""
    policy = RetentionPolicy()
    return {
        "full_retention_days": policy.keep_full_days,
        "sampled_retention_days": policy.keep_sampled_days,
        "sample_frames": policy.sample_frames,
        "sampling": "first and last frame always survive, because those are what an audit reads",
        "planning": "plan_prune is read-only; apply_prune is the only thing that deletes",
        "containment": "apply refuses any path that resolves outside the evidence root",
        "automatic": False,
        "why_not_automatic": (
            "deleting a customer's audit trail on a timer is not a decision to make behind their "
            "back; the plan is meant to be read first"
        ),
    }
