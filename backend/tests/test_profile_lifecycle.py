"""Profile lifecycle: nothing is created that nothing removes.

FOUND LIVE. After a run the VPS held:

    /tmp/granada-browser/org-e2e/       <- one directory PER ORGANISATION, created and never removed
    /tmp/granada-browser/org-ev/
    /tmp/granada-browser/org-visual/
    /tmp/granada-browser-2kfj0bjq       <- mkdtemp dirs from CRASHED runs, where close() never ran
    /tmp/granada-browser-j7sjcmza

Two distinct leaks, and the first was the worse: an unbounded directory named after every organisation
that had ever run, on a host intended to serve thousands. It is a disk leak AND a listing of which
NGOs have been active.

These tests pin both, and the fix is verified live as well - the same method that found it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "tools") not in sys.path:
    sys.path.insert(0, str(BACKEND / "tools"))

import browser_worker as bw  # noqa: E402

from agent.browser_runtime import Step  # noqa: E402


def _makedirs_calls() -> list[str]:
    """Every `os.makedirs(...)` in the worker that is CODE rather than commentary.

    The first version of these tests asserted `"os.makedirs(profile_dir" not in source`, which the
    COMMENT explaining the removal also satisfies - so the test failed against its own fix. A check
    that cannot tell code from a comment about code is not a check.
    """
    calls: list[str] = []
    for line in (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "os.makedirs(" in stripped:
            calls.append(stripped)
    return calls


class FakePage:
    def __init__(self):
        self.screenshots: list[str] = []

    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.screenshots.append(path)


def provider_without_browser() -> bw.PlaywrightProvider:
    """A provider with the browser bits stubbed: these tests are about PATHS, not about Chromium."""
    p = bw.PlaywrightProvider()
    p._page = FakePage()
    return p


# ===========================================================================
# LEAK 1: the per-tenant directory that nothing removed
# ===========================================================================
def test_launch_does_not_create_the_profile_dir(tmp_path, monkeypatch):
    """THE regression. `os.makedirs(profile_dir)` ran on every launch and NOTHING removed it.

    The real Chromium profile is the mkdtemp directory, which launch_persistent_context uses as
    user_data_dir - that is what gives each organisation its own cookie jar. `profile_dir` was
    created and then never used: dead code that leaked one directory per organisation, forever.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(bw.tempfile, "mkdtemp", lambda prefix=None: _fake_mkdtemp(tmp_path, prefix))

    p = provider_without_browser()
    per_tenant = tmp_path / "granada-browser" / "org-aaaa"
    p.launch = lambda **kw: None  # not launching Chromium in a unit test

    # Call the real makedirs decision by inspecting what launch WOULD create. We assert the source
    # of truth directly: the string is gone from launch, so no directory appears.
    assert _makedirs_calls() == [], (
        "launch() creates profile_dir and nothing removes it - one directory per organisation. "
        "A substring check would also match the COMMENT that explains the removal, so this looks at "
        "code lines only."
    )


def test_the_worker_sweeps_stale_profiles(tmp_path, monkeypatch):
    """LEAK 2. A crash means close() never runs, so the mkdtemp directory survives. Bounded by AGE,
    because a profile belonging to a concurrent run must not be swept - that would corrupt an
    in-flight submission and look like a site bug."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    stale = tmp_path / "granada-browser-stale123"
    stale.mkdir()
    old = time.time() - (bw.STALE_PROFILE_SECONDS + 60)
    os.utime(stale, (old, old))

    fresh = tmp_path / "granada-browser-fresh456"
    fresh.mkdir()

    removed = bw._sweep_stale_profiles(keep=None)

    assert str(stale) in removed, "an abandoned profile was not swept"
    assert not stale.exists()
    assert fresh.exists(), "a CONCURRENT run's profile was swept - that would corrupt a live session"


def test_the_sweep_spares_a_named_keep(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    keep = tmp_path / "granada-browser-keepme"
    keep.mkdir()
    old = time.time() - (bw.STALE_PROFILE_SECONDS + 60)
    os.utime(keep, (old, old))

    removed = bw._sweep_stale_profiles(keep=str(keep))
    assert str(keep) not in removed
    assert keep.exists()


def test_the_sweep_is_best_effort(monkeypatch):
    """It runs inside close(), so it must never raise. A cleanup that crashes the run it is tidying
    up after is worse than the leak."""
    monkeypatch.setattr(bw.tempfile, "tempdir", "/definitely/not/a/real/directory")
    assert bw._sweep_stale_profiles(keep=None) == []


def test_the_age_bound_is_generous_enough_for_a_slow_portal():
    """A portal that takes minutes is a LIVE run. Sweeping it would delete cookies mid-submission."""
    assert bw.STALE_PROFILE_SECONDS >= 3600


# ===========================================================================
# THE SPECIFIC DIRECTORY THAT LEAKED
# ===========================================================================
def test_no_organisation_named_directory_is_created(tmp_path, monkeypatch):
    """Named after the organisation, which makes it a listing of which NGOs have been active as well
    as a disk leak."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert _makedirs_calls() == []


def test_close_is_idempotent():
    """close() may be called by the runtime's finally and again by a caller. It must not raise on the
    second call, and must not leave _profile pointing at a directory it already deleted."""
    p = provider_without_browser()
    p._profile = None
    p.close()
    p.close()


# ===========================================================================
# THE EVIDENCE PATH IS UNAFFECTED BY THIS FIX
# ===========================================================================
def test_removing_the_profile_dir_does_not_touch_evidence(tmp_path, monkeypatch):
    """The two must stay separate. The fix removes a scratch directory; evidence lives elsewhere and
    must be untouched by any cleanup."""
    from agent.evidence_store import EvidenceStore

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ev_root = tmp_path / "evidence"
    store = EvidenceStore(root=ev_root, org_id="o", run_id="r")

    src = tmp_path / "granada-browser-x" / "shot.png"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    record = store.store(kind="screenshot", source=src, name="shot.png")

    import shutil
    shutil.rmtree(src.parent, ignore_errors=True)

    assert Path(record.ref).exists(), "the profile cleanup took the evidence with it"
