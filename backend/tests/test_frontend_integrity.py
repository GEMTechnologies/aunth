"""Frontend source integrity: nothing dead, nothing shadowed, nothing unresolvable.

WHAT THIS FOUND
---------------
Twenty-two module files under `src/`, of which **eight were never bundled**:

* **Seven were shadowed.** Webpack resolves `.tsx` before `.jsx`, and six of them were
  one-line `// Placeholder for X.jsx` files. The seventh, **`ProfilePage.jsx`, was not a
  placeholder** - it was a 13-line stub rendering *"My Profile / This is where users can view
  and edit their personal information"*. Shadowed today, but if the resolve order ever
  changed or somebody imported it by extension, the app would silently render a
  plausible-looking placeholder instead of the real page. A stub that looks like a working
  page is worse than a missing one.
* **Four more were placeholders with no sibling** (`api.js`, `bootstrap.js`, `store.js`, and
  a "Placeholder for" comment inside `LoginPage.tsx`).

**Proven dead, not assumed dead.** The bundle's SHA-256 was recorded before the removals and
compared after: byte-identical. Webpack is deterministic here, so an unchanged bundle is
proof that nothing reached those files.

WHY THE LAST CHECK EXISTS
-------------------------
Five modules are real, substantial code that nothing renders - `LoginPage`, `AuthForm`,
`ProfileForm`, `InviteUserModal`, `AnimatedBackground`. They look like superseded
implementations: each page was rewritten in place rather than edited, leaving the earlier
version behind.

**Those are not deleted here.** Deleting somebody's working code because it is currently
unreferenced is overreach - it may be about to be wired up. But letting it accumulate
silently is how a directory fills with code nobody can safely touch. So each must be *listed
with a reason*, and a NEW unreachable module fails this test until somebody decides.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/ -> backend/ -> Auth/. `parents[2]` is already Auth, so appending "Auth" again
# produced Auth/Auth/frontend and the whole scan found nothing.
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SRC = FRONTEND / "src"
ENTRY = SRC / "index.tsx"

#: The extension order webpack resolves, from `webpack.config.js`. Asserted against the real
#: config below, because a mismatch would make every shadowing check wrong.
RESOLVE_ORDER = (".tsx", ".ts", ".js", ".jsx")

#: Module files that are intentionally unreachable, each with the reason.
#:
#: Every entry is a decision. An empty-justified entry is not allowed: "unused" is not a
#: reason, it is the observation.
KNOWN_UNREACHABLE: dict[str, str] = {
    "pages/LoginPage.tsx": (
        "superseded by AuthPage, which handles login and registration in one screen. Kept "
        "pending a decision on whether the split screens return."
    ),
    "components/AuthForm.tsx": (
        "superseded by the form inside AuthPage, which needs the OAuth callback handling "
        "AuthForm does not have."
    ),
    "components/ProfileForm.tsx": (
        "superseded by the inline editor in ProfilePage. Kept because it is the only version "
        "with per-field validation."
    ),
    "components/InviteUserModal.tsx": (
        "there is no invitation flow in the API yet, so nothing can render it. Wire it up or "
        "remove it when invitations ship."
    ),
    "components/AnimatedBackground.tsx": (
        "visual only, and AuthPage now animates its own background with framer-motion."
    ),
}

#: Type-declaration files are not modules and are never imported.
TYPE_DECLARATIONS = (".d.ts",)

IMPORT_RE = re.compile(
    r"""(?:import\s[^;]*?from\s*|import\s*|export\s[^;]*?from\s*|require\s*\()\s*['"]([^'"]+)['"]"""
)


def _resolve(specifier: str, importer: Path) -> Path | None:
    """Resolve a relative import the way webpack does."""
    if not specifier.startswith("."):
        return None
    base = (importer.parent / specifier).resolve()
    candidates = [Path(str(base) + ext) for ext in RESOLVE_ORDER]
    candidates += [base / f"index{ext}" for ext in RESOLVE_ORDER]
    candidates.append(base)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _module_files() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*")
        if path.is_file()
        and path.suffix in RESOLVE_ORDER
        and not path.name.endswith(TYPE_DECLARATIONS)
    )


def _reachable() -> tuple[set[Path], list[tuple[str, str]]]:
    """Every module reachable from the entry point, and any relative import that failed."""
    seen: set[Path] = set()
    queue = [ENTRY]
    unresolved: list[tuple[str, str]] = []
    while queue:
        current = queue.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        text = current.read_text(encoding="utf-8", errors="ignore")
        for specifier in IMPORT_RE.findall(text):
            target = _resolve(specifier, current)
            if target is None:
                if specifier.startswith("."):
                    unresolved.append((str(current.relative_to(SRC)), specifier))
                continue
            queue.append(target)
    return seen, unresolved


# ===========================================================================
def test_the_entry_point_exists():
    """Everything below is measured from here; a missing entry would make the scan vacuous."""
    assert ENTRY.is_file(), f"webpack's entry point is missing: {ENTRY}"


def test_the_resolve_order_matches_webpack():
    """The shadowing checks are only meaningful if this matches the real config."""
    config = (FRONTEND / "webpack.config.js").read_text(encoding="utf-8")
    match = re.search(r"extensions:\s*\[(.*?)\]", config, re.DOTALL)
    assert match, "webpack.config.js declares no resolve.extensions"
    declared = tuple(
        part.strip().strip("'\"") for part in match.group(1).split(",") if part.strip()
    )
    assert declared == RESOLVE_ORDER, (
        f"webpack resolves {declared} but this file assumes {RESOLVE_ORDER}. Every "
        "shadowing result below is wrong until they agree."
    )


def test_no_module_is_shadowed_by_a_sibling():
    """THE trap that was in this repository.

    `ProfilePage.jsx` was a real 13-line stub rendering a plausible profile page, shadowed by
    `ProfilePage.tsx`. If the resolve order changed, or anything imported it by extension, the
    app would silently render the placeholder - and a stub that looks like a working page is
    worse than a missing one, because nobody investigates a page that renders.
    """
    by_stem: dict[Path, list[Path]] = {}
    for path in _module_files():
        by_stem.setdefault(path.with_suffix(""), []).append(path)

    shadowed = {
        stem: [p.name for p in paths]
        for stem, paths in by_stem.items()
        if len(paths) > 1
    }
    assert not shadowed, (
        "these modules are shadowed by a sibling with a higher-priority extension, so they "
        f"are dead code that still looks live: {sorted(shadowed.items())}"
    )


def test_no_placeholder_files_remain():
    """A file whose entire content is `// Placeholder for X` is a file that compiles, ships
    and does nothing. Two of them were `store.js` and `bootstrap.js` - names that suggest a
    state store and an app bootstrap, neither of which exists."""
    offenders = [
        str(path.relative_to(SRC))
        for path in _module_files()
        if path.read_text(encoding="utf-8", errors="ignore").strip().startswith(
            "// Placeholder for"
        )
    ]
    assert not offenders, f"placeholder files are still present: {offenders}"


def test_every_relative_import_resolves():
    """An import that does not resolve is a build error waiting for the branch that reaches
    it - and nothing here imports a file that does not exist."""
    _seen, unresolved = _reachable()
    assert not unresolved, (
        "these relative imports resolve to nothing: "
        + "; ".join(f"{importer} -> {spec!r}" for importer, spec in unresolved)
    )


def test_every_unreachable_module_is_a_DECISION_not_an_accumulation():
    """The check that keeps this from growing back.

    Five modules are real code that nothing renders. They are not deleted, because deleting
    somebody's working code for being currently unreferenced is overreach. But each must be
    listed in `KNOWN_UNREACHABLE` with a reason - so a NEW one fails this test until somebody
    decides, rather than joining a pile nobody can safely touch.
    """
    reachable, _unresolved = _reachable()
    everything = set(_module_files())
    unreachable = sorted(
        str(path.relative_to(SRC)).replace("\\", "/") for path in everything - reachable
    )

    undeclared = [name for name in unreachable if name not in KNOWN_UNREACHABLE]
    assert not undeclared, (
        f"these modules are not reachable from {ENTRY.name} and are not declared:\n  "
        + "\n  ".join(undeclared)
        + "\n\nEither wire them up, delete them, or add them to KNOWN_UNREACHABLE with a "
        "reason. An unreachable module that is nobody's decision is one nobody can safely "
        "change."
    )


def test_the_declared_unreachable_list_is_accurate_and_justified():
    """A stale list is a list nobody reads.

    An entry that is now reachable should be removed, and an entry whose reason is vacuous
    should not exist - "unused" is the observation, not the justification.
    """
    reachable, _unresolved = _reachable()
    now_reachable = [
        name
        for name in KNOWN_UNREACHABLE
        if (SRC / name) in reachable
    ]
    assert not now_reachable, (
        f"these are declared unreachable but ARE reachable now: {now_reachable}. Remove them "
        "from KNOWN_UNREACHABLE."
    )

    for name, reason in KNOWN_UNREACHABLE.items():
        assert (SRC / name).is_file(), f"{name} is declared but does not exist"
        assert len(reason.strip()) >= 40, (
            f"{name}'s reason is too short to be a decision: {reason!r}"
        )
        assert reason.strip().casefold() not in {"unused", "dead code", "not used"}, (
            f"{name}'s reason restates the observation instead of justifying it"
        )


def test_the_scan_is_not_vacuous():
    """So every assertion above cannot pass by finding nothing.

    A reachability walk that reached only the entry point would satisfy 'no unreachable
    modules are undeclared' trivially if the list were empty - and would report a dozen
    undeclared files otherwise. This pins the shape of a healthy result.
    """
    reachable, _unresolved = _reachable()
    assert len(reachable) >= 10, (
        f"only {len(reachable)} modules are reachable from the entry point; the walk is broken"
    )
    assert ENTRY in reachable
    assert (SRC / "App.tsx") in reachable, "App.tsx is not reachable, which cannot be right"
    assert (SRC / "pages" / "AgentPage.tsx") in reachable, (
        "the Agent screen is not reachable from the entry point"
    )
