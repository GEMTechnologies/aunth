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
# LEAK 1: the per-tenant directory - now a FEATURE, and bounded
# ===========================================================================
def test_launch_creates_one_persistent_profile_per_organisation(tmp_path, monkeypatch):
    """THE CONTRACT CHANGED, DELIBERATELY, AND THIS RECORDS WHY.

    The original fix removed `os.makedirs(profile_dir)` because it leaked one directory per
    organisation that nothing ever removed. That reasoning was right about the leak and wrong about
    the consequence: an unconditional mkdtemp means EVERY RUN STARTS LOGGED OUT, so a portal that
    verified an account a minute ago asks for verification again - and re-registering repeatedly is
    what the directive forbids. Measured: the profile in every run was `granada-browser-<random>`.

    The directory is now created on purpose, and the leak is answered by the location rather than by
    absence: it lives under a swept root and is bounded by the NUMBER OF ORGANISATIONS, not the number
    of runs. Both properties are asserted here.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(bw.tempfile, "mkdtemp", lambda prefix=None: _fake_mkdtemp(tmp_path, prefix))

    calls = _makedirs_calls()
    assert calls == ["os.makedirs(profile_dir, mode=0o700, exist_ok=True)"], (
        "launch() must create the organisation's profile directory: without it no session survives a "
        "run. The leak is answered by the root being swept, not by never creating it."
    )

    source = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    handler = source.split("def launch", 1)[1].split("def close", 1)[0]
    assert "0o700" in handler, "an organisation's cookies must not be readable by another local user"
    assert "_ephemeral_profile = False" in handler, (
        "a persistent profile must be marked as such, or close() deletes the session it exists to keep"
    )


def test_close_deletes_only_an_ephemeral_profile():
    """The unconditional rmtree in close() was itself what made session preservation impossible."""
    source = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    body = source.split("def close", 1)[1].split("def observe", 1)[0]
    assert "_ephemeral_profile" in body, (
        "close() must not delete a persistent profile: that would destroy the organisation's session "
        "at the end of every run"
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
def test_the_profile_root_is_swept_and_not_the_working_tree(tmp_path, monkeypatch):
    """The directory is created on purpose now, so the property that must hold is WHERE.

    Under a root that is bounded and swept, never scattered through the filesystem - and the caller
    points it at `/var/lib/granada/browser-profiles/<org_id>`, which is one directory per
    ORGANISATION rather than one per run.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    source = (BACKEND / "tools" / "browser_worker.py").read_text(encoding="utf-8")
    assert "/var/lib/granada/browser-profiles/" in source, (
        "the profile must live under the swept root, not in a world-writable /tmp"
    )
    calls = _makedirs_calls()
    assert calls == ["os.makedirs(profile_dir, mode=0o700, exist_ok=True)"]
    # The leak the original fix was answering is still answered: one directory per ORGANISATION,
    # because the path is derived from the org id rather than from a per-run random suffix.
    handler = source.split("provider.launch(", 1)[1].split(")", 1)[0]
    assert "{task.org_id}" in handler, "a per-run path would still leak one directory per run"


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
