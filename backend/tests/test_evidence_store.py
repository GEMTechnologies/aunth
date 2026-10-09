"""The evidence store, and the dangling reference it was built to fix.

FOUND LIVE, NOT HYPOTHETICALLY: a run captured 16,982-byte PNGs with valid magic bytes, referenced
them in its outcome, and left ZERO files on disk. They were written into the per-tenant Chromium
profile directory, and `close()` deletes that - correctly, because it holds cookies. The scratch
cleanup took the evidence with it.

An evidence reference that does not resolve is worse than no reference: it LOOKS like evidence.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.evidence_store import (  # noqa: E402
    MAX_FILES_PER_RUN,
    EvidenceError,
    EvidenceStore,
    _safe_name,
    _segment,
    describe,
    open_store,
)



@pytest.fixture
def evidence_root(tmp_path):
    """The EVIDENCE area - deliberately a SIBLING of scratch, not a child of it.

    The first version of this fixture put evidence under the same tmp_path as the scratch it then
    deleted, so the test destroyed its own evidence and failed - reproducing, inside the test, the
    exact defect the module exists to prevent. The separation has to exist in the fixture too.
    """
    d = tmp_path / "evidence"
    d.mkdir()
    return d


@pytest.fixture
def store(evidence_root):
    return EvidenceStore(root=evidence_root, org_id="org-aaaa", run_id="run-1")


def shot(tmp_path, name="shot.png", data=b"\x89PNG\r\n\x1a\nfake"):
    """Write into a scratch SUBDIRECTORY, so a test can delete the scratch without deleting the
    evidence root. Keeping the two under one directory was the mistake the first fixture made."""
    d = tmp_path / "scratch"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(data)
    return p


# ===========================================================================
# THE DEFECT
# ===========================================================================
def test_stored_evidence_STILL_EXISTS_after_the_source_is_destroyed(store, tmp_path):
    """THE test. This is exactly the production sequence: the browser writes into scratch, the run
    references it, and the scratch directory is deleted."""
    source = shot(tmp_path)
    record = store.store(kind="screenshot", source=source, name="shot.png")

    # The scratch cleanup, simulated exactly as the provider does it.
    import shutil
    shutil.rmtree(tmp_path / "scratch", ignore_errors=True)

    assert Path(record.ref).exists(), "evidence did not survive the scratch cleanup"
    assert not source.exists(), "the source should be gone - that is the point"


def test_the_reference_is_not_inside_the_scratch_area(store, tmp_path):
    """If evidence lived under the profile directory, cleanup would take it. It must not."""
    source = shot(tmp_path)
    record = store.store(kind="screenshot", source=source, name="shot.png")
    assert str(tmp_path / "scratch") not in record.ref
    assert str(store.root) in record.ref


def test_storing_a_file_that_does_not_exist_is_REFUSED(store, tmp_path):
    """A reference to a file that was never written is the defect itself - silently returning one
    would recreate it."""
    with pytest.raises(EvidenceError) as e:
        store.store(kind="screenshot", source=tmp_path / "scratch" / "nope.png", name="nope.png")
    assert "does not exist" in str(e.value)


# ===========================================================================
# TENANT SEPARATION IN THE PATH
# ===========================================================================
def test_two_organisations_cannot_collide_on_a_filename(tmp_path):
    a = EvidenceStore(root=tmp_path / "ev", org_id="org-a", run_id="r")
    b = EvidenceStore(root=tmp_path / "ev", org_id="org-b", run_id="r")
    src = shot(tmp_path, "same.png")

    ra = a.store(kind="screenshot", source=src, name="same.png")
    rb = b.store(kind="screenshot", source=src, name="same.png")

    assert ra.ref != rb.ref, "identical filenames from different tenants shared a path"
    assert "org-a" in ra.ref and "org-b" in rb.ref


def test_the_run_appears_in_the_path_too(tmp_path):
    s1 = EvidenceStore(root=tmp_path / "ev", org_id="o", run_id="run-1")
    s2 = EvidenceStore(root=tmp_path / "ev", org_id="o", run_id="run-2")
    src = shot(tmp_path, "x.png")
    assert s1.store(kind="s", source=src, name="x.png").ref != s2.store(kind="s", source=src, name="x.png").ref


# ===========================================================================
# TRAVERSAL
# ===========================================================================
def test_a_traversing_filename_cannot_escape(store, tmp_path):
    """§10 requires path traversal rejection, and a filename may come from a page title or a donor's
    document name - both attacker-influenced."""
    source = shot(tmp_path, "payload.png")
    record = store.store(kind="screenshot", source=source, name="../../../etc/passwd.png")
    resolved = Path(record.ref).resolve()
    assert store.directory().resolve() in resolved.parents, f"escaped to {resolved}"


def test_a_traversing_org_id_cannot_escape(tmp_path):
    s = EvidenceStore(root=tmp_path / "ev", org_id="../../etc", run_id="r")
    assert ".." not in str(s.directory())


def test_a_traversing_run_id_cannot_escape(tmp_path):
    s = EvidenceStore(root=tmp_path / "ev", org_id="o", run_id="../../../root")
    assert ".." not in str(s.directory())


def test_an_empty_segment_becomes_a_placeholder_not_an_empty_path_component():
    """An empty component would silently collapse the path and put two tenants in one directory."""
    assert _segment("") == "unknown"
    assert _segment("...") == "unknown"
    assert _segment("   ") == "unknown"


def test_an_empty_name_becomes_a_real_filename():
    assert _safe_name("", kind="screenshot").endswith(".bin")
    assert _safe_name("...", kind="screenshot")


def test_separators_in_a_name_do_not_create_directories(store, tmp_path):
    source = shot(tmp_path, "s.png")
    record = store.store(kind="screenshot", source=source, name="a/b/c.png")
    assert Path(record.ref).parent == store.directory()


# ===========================================================================
# INTEGRITY
# ===========================================================================
def test_a_record_carries_a_checksum(store, tmp_path):
    record = store.store(kind="screenshot", source=shot(tmp_path, data=b"hello"), name="x.png")
    assert record.checksum == hashlib.sha256(b"hello").hexdigest()


def test_verify_reports_a_modified_artefact(store, tmp_path):
    """So a later reader can show the file is the one referenced, not merely one sharing a name."""
    record = store.store(kind="screenshot", source=shot(tmp_path, data=b"hello"), name="x.png")
    assert store.verify() == []

    Path(record.ref).write_bytes(b"tampered")
    problems = store.verify()
    assert problems and "checksum mismatch" in problems[0]


def test_verify_reports_a_deleted_artefact(store, tmp_path):
    record = store.store(kind="screenshot", source=shot(tmp_path), name="x.png")
    Path(record.ref).unlink()
    assert store.verify() == [record.ref]


# ===========================================================================
# BOUNDS
# ===========================================================================
def test_the_file_count_is_bounded(store, tmp_path):
    """A page that keeps producing screenshots is the same unbounded-work problem the runtime bounds
    for actions."""
    src = shot(tmp_path, "s.png")
    for i in range(MAX_FILES_PER_RUN):
        store.store(kind="screenshot", source=src, name=f"s{i}.png")
    with pytest.raises(EvidenceError) as e:
        store.store(kind="screenshot", source=src, name="one-too-many.png")
    assert "bound is" in str(e.value)


def test_the_byte_total_is_bounded(tmp_path):
    s = EvidenceStore(root=tmp_path / "ev", org_id="o", run_id="r", max_total_bytes=50)
    src = shot(tmp_path, "big.png", data=b"x" * 40)
    s.store(kind="s", source=src, name="a.png")
    with pytest.raises(EvidenceError):
        s.store(kind="s", source=src, name="b.png")


def test_total_bytes_is_tracked(store, tmp_path):
    store.store(kind="s", source=shot(tmp_path, "a.png", data=b"12345"), name="a.png")
    store.store(kind="s", source=shot(tmp_path, "b.png", data=b"123"), name="b.png")
    assert store.total_bytes() == 8


# ===========================================================================
# THE SOURCE IS NOT CONSUMED
# ===========================================================================
def test_storing_copies_rather_than_moves(store, tmp_path):
    """The run may still need the original, and a store that removed it would break the caller it
    was meant to help."""
    source = shot(tmp_path, data=b"original")
    store.store(kind="s", source=source, name="x.png")
    assert source.exists()
    assert source.read_bytes() == b"original"


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_separation_and_what_it_does_not_do():
    d = describe()
    assert "profile directory" in d["separate_from"]
    assert "cookies" in d["separate_from"]
    assert "cannot collide" in d["tenant_scoped_path"]
    assert "replaced, not escaped" in d["traversal"]
    joined = " ".join(d["does_not_do"])
    assert "retention is a separate decision" in joined


def test_open_store_reads_the_environment_root(monkeypatch, tmp_path):
    monkeypatch.setenv("GRANADA_EVIDENCE_ROOT", str(tmp_path / "from-env"))
    s = open_store(org_id="o", run_id="r")
    assert s.root == tmp_path / "from-env"
