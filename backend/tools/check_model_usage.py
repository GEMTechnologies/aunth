"""Static check for code that references model attributes which do not exist.

Phase 1 kept finding this same bug in different clothes: service code written
against an older version of the schema. ``OrgMember`` was refactored to carry
``org_id`` and a ``role_id`` foreign key, but callers still passed
``organisation_id=`` and ``role=``. That only fails when the line executes.

Import and runtime tests do not catch it, because an unused branch stays
unused. This walks the AST of every module and reports each dotted reference
whose final component is not a real column or relationship on the class.

Run directly for a report:

    python tools/check_model_usage.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402

SKIP_DIRS = {".venv", "__pycache__", "tests", "alembic", "migrations"}


def model_attributes() -> dict[str, set[str]]:
    """Map every mapped class to the attributes it actually declares."""
    catalogue: dict[str, set[str]] = {}
    for name, obj in vars(models).items():
        mapper = getattr(obj, "__mapper__", None)
        if mapper is None:
            continue
        attrs = set(mapper.columns.keys())
        attrs.update(r.key for r in mapper.relationships)
        attrs.update(vars(obj))
        catalogue[name] = attrs
    return catalogue


def dotted(node: ast.AST) -> list[str] | None:
    """Flatten ``a.b.c`` into ["a","b","c"]; None for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def check_file(path: pathlib.Path, catalogue: dict[str, set[str]]) -> list[tuple[int, str, str]]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [(0, "<module>", f"UNPARSEABLE: {exc}")]

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        # models.OrgMember(organisation_id=..., role=...) -> validate kwargs
        if isinstance(node, ast.Call):
            callee = dotted(node.func)
            if callee and len(callee) == 2 and callee[0] == "models":
                cls_name = callee[1]
                if cls_name in catalogue:
                    valid = catalogue[cls_name]
                    for kw in node.keywords:
                        if kw.arg is None or kw.arg in valid or kw.arg == "self":
                            continue
                        if kw.arg.startswith("_"):
                            continue
                        findings.append(
                            (node.lineno, cls_name, f"models.{cls_name}({kw.arg}=...)")
                        )

        if not isinstance(node, ast.Attribute):
            continue
        parts = dotted(node)
        if not parts or len(parts) < 2:
            continue

        # models.OrgMember.organisation_id  ->  class OrgMember
        cls_name = None
        if parts[0] == "models" and parts[1] in catalogue:
            cls_name = parts[1]
            attribute = ".".join(parts[2:])
        elif parts[0] in catalogue:
            cls_name = parts[0]
            attribute = ".".join(parts[1:])
        if cls_name is None:
            continue

        head = attribute.split(".")[0]
        if not head or head in catalogue[cls_name]:
            continue
        findings.append((node.lineno, cls_name, ".".join(parts)))

    return findings


def main() -> int:
    catalogue = model_attributes()
    total = 0

    for path in sorted(BACKEND.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        for lineno, cls_name, reference in check_file(path, catalogue):
            rel = path.relative_to(BACKEND)
            print(f"{rel}:{lineno}: {cls_name} has no attribute used by {reference}")
            total += 1

    print(f"\n{total} bad model reference(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())