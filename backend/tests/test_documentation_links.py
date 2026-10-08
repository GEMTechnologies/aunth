"""Every documentation link the operational tooling points at must resolve.

THE DEFECT THIS FIXES
---------------------
`ops/prometheus/granada.rules.yml` carried four `runbook:` links, and **all four were dead -
twice over**:

1. **Wrong path.** They pointed at `docs/DEPLOYMENT.md`; the file lived at the repository
   root. The alert an on-call engineer clicks at 03:00 landed on nothing.
2. **Wrong anchors.** `#monitoring` and `#long-running-processes` did not match the real
   headings `#6b-monitoring` and `#4-the-long-running-processes`, because a Markdown anchor is
   generated from the WHOLE heading - a numbered heading needs its number in the fragment. And
   **`#submission` had no heading at all**, so the runbook for the one alert with a real
   deadline did not exist.

Neither failure raises anything. A link is a string; nothing validates it; and the only way to
discover it is to need it.

WHY THE ANCHOR ALGORITHM IS REPRODUCED HERE
-------------------------------------------
It is not GitHub's exactly, and it does not need to be. What matters is that a heading and its
fragment are derived from each other, so a heading that changes breaks the link that points at
it. A guard that accepted any fragment would catch the path half and miss the half that was
actually wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: Files that point at documentation. Anything here must resolve.
REFERRING_FILES = (
    ROOT / "ops" / "prometheus" / "granada.rules.yml",
    ROOT / "ops" / "systemd" / "granada-fleet.service",
    ROOT / "ops" / "systemd" / "granada-outbox-relay.service",
)

LINK_RE = re.compile(r"(?:runbook:\s*|Documentation=file:///opt/granada/)(\S+)")


def _anchor(heading: str) -> str:
    """The fragment a Markdown renderer generates for a heading.

    Lowercased, punctuation dropped, spaces to hyphens. Enough to make a heading and its
    fragment move together.
    """
    text = heading.lstrip("#").strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).lower()


def _headings(path: Path) -> set[str]:
    anchors = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            anchors.add(_anchor(line))
    return anchors


def _links() -> list[tuple[str, str, str]]:
    """`(referring file, target path, anchor or "")` for every operational doc link."""
    found: list[tuple[str, str, str]] = []
    for path in REFERRING_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = LINK_RE.search(line)
            if not match:
                continue
            target = match.group(1).strip()
            if target.startswith("http"):
                continue
            doc, _, anchor = target.partition("#")
            found.append((str(path.relative_to(ROOT)), doc, anchor))
    return found


# ===========================================================================
def test_the_scan_finds_the_links():
    """So the assertions below cannot pass by finding nothing."""
    links = _links()
    assert len(links) >= 6, f"only found {len(links)} documentation links; the scan is broken"


def test_every_referenced_document_exists():
    """THE path half. A runbook link that resolves to nothing is worse than no link, because
    it looks like guidance exists."""
    missing = []
    for referring, doc, _anchor in _links():
        if not (ROOT / doc).is_file():
            missing.append(f"{referring} -> {doc}")
    assert not missing, (
        "these operational links point at documents that do not exist:\n  " + "\n  ".join(missing)
    )


def test_every_referenced_anchor_exists():
    """THE anchor half, which is the one that was actually wrong.

    `#monitoring` does not reach `## 6b. Monitoring`, and `#submission` reached nothing at all.
    A Markdown anchor comes from the whole heading, so a numbered heading carries its number.
    """
    missing = []
    for referring, doc, anchor in _links():
        if not anchor:
            continue
        path = ROOT / doc
        if not path.is_file():
            continue
        if anchor not in _headings(path):
            available = sorted(_headings(path))
            near = [a for a in available if anchor.split("-")[0] in a]
            missing.append(
                f"{referring} -> {doc}#{anchor}"
                + (f"  (closest: {near[:3]})" if near else "")
            )
    assert not missing, (
        "these operational links point at headings that do not exist, so an on-call engineer "
        "following them lands nowhere:\n  " + "\n  ".join(missing)
    )


def test_the_anchor_derivation_is_not_trivially_permissive():
    """A guard that accepted any fragment would catch the path half and miss the real defect.

    Asserted against the two headings whose anchors were wrong, so the algorithm is pinned to
    the thing it was written for.
    """
    deployment = ROOT / "docs" / "DEPLOYMENT.md"
    if not deployment.is_file():
        pytest.skip("docs/DEPLOYMENT.md is absent")
    anchors = _headings(deployment)
    assert "6b-monitoring" in anchors
    assert "4-the-long-running-processes" in anchors
    assert "6c-submission" in anchors
    # And the fragments that were wrong are still wrong, so the test above is meaningful.
    assert "monitoring" not in anchors
    assert "long-running-processes" not in anchors


def test_the_systemd_units_point_at_the_runbook_the_brief_names():
    """The brief requires `docs/RUNBOOK.md`, and a unit file is deployed configuration: a link
    there is a link an operator follows on the host."""
    for unit in ("granada-fleet.service", "granada-outbox-relay.service"):
        text = (ROOT / "ops" / "systemd" / unit).read_text(encoding="utf-8")
        assert "docs/RUNBOOK.md" in text, f"{unit} does not point at the required runbook path"
        assert "RUNBOOK_LOCAL" not in text, (
            f"{unit} still points at the pre-move filename, which no longer exists"
        )


# ===========================================================================
# THE BRIEF'S REQUIRED DELIVERABLES
# ===========================================================================
#: Named explicitly by the brief. Asserted because THREE of them were missing, stale or at the
#: wrong path for several phases and nothing noticed:
#:
#:   * `docs/EMAIL_ARCHITECTURE.md` did not exist at all;
#:   * `DEPLOYMENT.md` and `RUNBOOK_LOCAL.md` sat at the repository ROOT, so every
#:     `docs/DEPLOYMENT.md` reference in the alerting rules resolved to nothing;
#:   * `docs/CURRENT_STATE.md` described Phase 0/1 for thirteen phases while the system moved
#:     through Phase 14.
#:
#: A deliverable list in a brief is a requirement, and a requirement nothing checks is a
#: requirement that quietly stops being met.
REQUIRED_DELIVERABLES = (
    "docs/AGENTIC_ARCHITECTURE.md",
    "docs/CURRENT_STATE.md",
    "docs/BOT_INGESTION_CONTRACT.md",
    "docs/EVENT_CATALOG.md",
    "docs/DATA_MODEL.md",
    "docs/EMAIL_ARCHITECTURE.md",
    "docs/AUTONOMY_POLICY.md",
    "docs/SECURITY_REMEDIATION.md",
    "docs/DEPLOYMENT.md",
    "docs/RUNBOOK.md",
    "CHANGELOG_AGENTIC.md",
)


def test_every_required_deliverable_exists():
    """THE guard for a requirement that had gone unmet without anybody knowing."""
    missing = [rel for rel in REQUIRED_DELIVERABLES if not (ROOT / rel).is_file()]
    assert not missing, (
        "the brief requires these documents and they do not exist:\n  " + "\n  ".join(missing)
    )


def test_the_adr_directory_holds_decisions():
    """`docs/DECISIONS/` is required for ADRs, and an empty directory satisfies 'exists' while
    documenting nothing."""
    adrs = sorted((ROOT / "docs" / "DECISIONS").glob("ADR-*.md"))
    assert len(adrs) >= 10, f"only {len(adrs)} ADRs are present"
    for adr in adrs:
        # An ADR with no structure is a note, not a decision.
        text = adr.read_text(encoding="utf-8")
        assert len(text) >= 400, f"{adr.name} is too short to record a decision"


def test_the_current_state_document_is_not_stale():
    """It described Phase 0/1 for thirteen phases.

    Checked against the migration head rather than a date, because a document is stale when it
    describes a system that no longer exists - and the revision is the cheapest signal of that.
    """
    current = (ROOT / "docs" / "CURRENT_STATE.md").read_text(encoding="utf-8")
    versions = sorted(
        p.name.split("_")[0]
        for p in (ROOT / "Auth" / "backend" / "alembic" / "versions").glob("*.py")
        if p.name[0].isdigit()
    )
    head = versions[-1]
    assert "018" in current or head in current, (
        f"docs/CURRENT_STATE.md does not mention the migration head ({head}); it is describing "
        "a system that no longer exists"
    )
    # And it must not still claim the Phase 0/1 state.
    for stale in ("098 passed", "Does the auth service start? | **No.**", "Phase 1 **committed but incomplete**"):
        assert stale not in current, (
            f"docs/CURRENT_STATE.md still contains the Phase 0/1 claim {stale!r}"
        )


def test_the_root_pointer_does_not_contradict_its_target():
    """The root file is a pointer, and it said "Does the auth service start? No." for thirteen
    phases while the service ran. A pointer that contradicts its target is worse than none."""
    pointer = ROOT / "CURRENT_STATE.md"
    if not pointer.is_file():
        pytest.skip("no root pointer document")
    text = pointer.read_text(encoding="utf-8")
    assert "docs/CURRENT_STATE.md" in text, "the pointer does not point at the canonical file"
    assert "1181" in text or "passed" in text, (
        "the pointer reports no test result, so it cannot be checked against the suite"
    )
    assert "Does the auth service start? | **No.**" not in text
