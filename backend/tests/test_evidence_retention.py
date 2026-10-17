"""Evidence retention: what a prune would remove, and what it must never touch.

The assertions that matter are about SAFETY, not about counting files. A retention sweep that
deletes one file too many destroys an audit trail; one that escapes the evidence root deletes
somebody else's data. So the tests are organised around the three properties the module claims:

  1. planning is read-only - a dry run can never be the thing that lost the files;
  2. the first and last frames survive down-sampling, because those are what an audit reads;
  3. apply refuses any path outside the root, even if the plan says otherwise.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.evidence_retention import (
    RetentionError,
    RetentionPolicy,
    apply_prune,
    plan_prune,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def make_run(root: Path, *, org="org-1", run="run-1", frames=10, age_days=0, size=100):
    """A run directory shaped exactly as `EvidenceStore.directory()` produces."""
    directory = root / org / run
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(days=age_days)).timestamp()
    for index in range(frames):
        path = directory / f"shot-{index:03d}.png"
        path.write_bytes(b"x" * size)
        os.utime(path, (stamp, stamp))
    return directory


def names(entries):
    return sorted(entry.path.name for entry in entries)


# ---------------------------------------------------------------------------
# Politeness: nothing is created inside the retention window
# ---------------------------------------------------------------------------
def test_a_fresh_run_is_left_alone(tmp_path):
    make_run(tmp_path, age_days=1)
    plan = plan_prune(tmp_path, now=NOW)
    assert plan.delete == []
    assert len(plan.keep) == 10


def test_planning_never_touches_the_disk(tmp_path):
    """A dry run that loses files would make the whole plan-then-apply split pointless."""
    directory = make_run(tmp_path, age_days=200)
    before = sorted(p.name for p in directory.iterdir())
    plan = plan_prune(tmp_path, now=NOW)
    assert len(plan.delete) == 10, "the plan should want to release an ancient run"
    assert sorted(p.name for p in directory.iterdir()) == before, "planning must be read-only"


# ---------------------------------------------------------------------------
# Down-sampling: thin first, keep the frames an audit reads
# ---------------------------------------------------------------------------
def test_a_run_inside_the_sampled_window_is_thinned_not_deleted(tmp_path):
    make_run(tmp_path, frames=20, age_days=40)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(sample_frames=4), now=NOW)
    assert len(plan.keep) == 4
    assert len(plan.delete) == 16
    assert plan.runs_downsampled == 1
    assert plan.runs_released == 0


def test_the_first_and_last_frames_always_survive(tmp_path):
    make_run(tmp_path, frames=20, age_days=40)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(sample_frames=4), now=NOW)
    kept = names(plan.keep)
    assert "shot-000.png" in kept, "the first frame is what the run started from"
    assert "shot-019.png" in kept, "the last frame is how it ended"


def test_the_sample_is_spread_rather_than_clustered(tmp_path):
    make_run(tmp_path, frames=100, age_days=40)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(sample_frames=4), now=NOW)
    indexes = sorted(int(n.split("-")[1].split(".")[0]) for n in names(plan.keep))
    assert indexes == [0, 33, 66, 99]


def test_a_run_smaller_than_the_sample_is_untouched(tmp_path):
    make_run(tmp_path, frames=3, age_days=40)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(sample_frames=4), now=NOW)
    assert plan.delete == []
    assert len(plan.keep) == 3


# ---------------------------------------------------------------------------
# Release: beyond the sampled window the run goes
# ---------------------------------------------------------------------------
def test_an_ancient_run_is_released_entirely(tmp_path):
    make_run(tmp_path, frames=6, age_days=200)
    plan = plan_prune(tmp_path, now=NOW)
    assert len(plan.delete) == 6
    assert plan.keep == []
    assert plan.runs_released == 1


def test_apply_deletes_exactly_the_plan(tmp_path):
    directory = make_run(tmp_path, frames=6, age_days=200)
    plan = plan_prune(tmp_path, now=NOW)
    result = apply_prune(plan, root=tmp_path)
    assert result.deleted == 6
    assert result.failed == []
    assert list(directory.iterdir()) == [], "the released run is empty"


def test_a_downsample_leaves_the_kept_frames_on_disk(tmp_path):
    directory = make_run(tmp_path, frames=20, age_days=40)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(sample_frames=4), now=NOW)
    apply_prune(plan, root=tmp_path)
    remaining = sorted(p.name for p in directory.iterdir())
    assert len(remaining) == 4
    assert "shot-000.png" in remaining and "shot-019.png" in remaining


# ---------------------------------------------------------------------------
# Containment: the property that makes this safe to run at all
# ---------------------------------------------------------------------------
def test_apply_refuses_a_path_outside_the_root(tmp_path):
    """`plan` is data a caller could construct by hand, so apply must not trust it. A prune that
    followed a path out of the evidence directory is data loss of the worst kind."""
    outside = tmp_path / "important.txt"
    outside.write_text("must survive")
    root = tmp_path / "evidence"
    root.mkdir()

    from agent.evidence_retention import FileEntry, PrunePlan

    plan = PrunePlan(delete=[FileEntry(path=outside, size=14, mtime=NOW)])
    result = apply_prune(plan, root=root)

    assert result.deleted == 0
    assert result.failed and "outside the evidence root" in result.failed[0]
    assert outside.exists(), "the file outside the root must be untouched"


def test_symlinks_are_not_followed(tmp_path):
    """A symlink inside a run directory could otherwise point anywhere; it is skipped at scan time
    and refused at apply time."""
    root = tmp_path / "evidence"
    directory = make_run(root, frames=2, age_days=200)
    target = tmp_path / "outside.txt"
    target.write_text("do not delete me")
    link = directory / "shot-999.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    plan = plan_prune(root, now=NOW)
    assert all(not entry.path.is_symlink() for entry in plan.delete)
    apply_prune(plan, root=root)
    assert target.exists()


# ---------------------------------------------------------------------------
# The hard cap
# ---------------------------------------------------------------------------
def test_a_byte_cap_releases_the_oldest_runs_first(tmp_path):
    """Disk exhaustion is a worse outcome than a short history, but the cap must never break the
    full-retention promise for a fresh run."""
    make_run(tmp_path, run="old", frames=5, age_days=20, size=1000)
    make_run(tmp_path, run="fresh", frames=5, age_days=1, size=1000)
    policy = RetentionPolicy(max_total_bytes=5000)
    plan = plan_prune(tmp_path, policy=policy, now=NOW)
    assert sum(f.size for f in plan.keep) <= 5000
    released_runs = {entry.path.parent.name for entry in plan.delete}
    assert "old" in released_runs


def test_a_cap_larger_than_the_store_changes_nothing(tmp_path):
    make_run(tmp_path, frames=4, age_days=1)
    plan = plan_prune(tmp_path, policy=RetentionPolicy(max_total_bytes=10**9), now=NOW)
    assert plan.delete == []


# ---------------------------------------------------------------------------
# Empty and hostile roots
# ---------------------------------------------------------------------------
def test_a_missing_root_is_not_an_error(tmp_path):
    plan = plan_prune(tmp_path / "does-not-exist", now=NOW)
    assert plan.runs_scanned == 0 and plan.delete == []


def test_an_empty_store_is_not_an_error(tmp_path):
    assert plan_prune(tmp_path, now=NOW).delete == []


def test_an_empty_run_directory_is_ignored(tmp_path):
    (tmp_path / "org-1" / "run-1").mkdir(parents=True)
    plan = plan_prune(tmp_path, now=NOW)
    assert plan.delete == []


def test_stray_files_at_the_root_are_not_treated_as_evidence(tmp_path):
    """Walking deeper than `root/<org>/<run>` would let a stray copy of somebody's filesystem be
    pruned as if it were evidence."""
    (tmp_path / "loose-file.png").write_bytes(b"x" * 10)
    plan = plan_prune(tmp_path, now=NOW)
    assert plan.delete == []


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------
def test_a_sampled_window_shorter_than_the_full_window_is_refused():
    with pytest.raises(RetentionError):
        RetentionPolicy(keep_full_days=30, keep_sampled_days=10)


def test_a_single_sample_frame_is_refused():
    with pytest.raises(RetentionError):
        RetentionPolicy(sample_frames=1)


def test_negative_windows_are_refused():
    with pytest.raises(RetentionError):
        RetentionPolicy(keep_full_days=-1)


def test_the_plan_reports_itself():
    payload = plan_prune(Path("/nonexistent-root-for-test")).as_dict()
    assert payload["files_to_delete"] == 0
    assert payload["bytes_freed"] == 0
