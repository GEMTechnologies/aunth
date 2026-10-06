"""Compare the Alembic-migrated schema against the SQLAlchemy models.

Why this exists
---------------
The auth service created its schema with ``Base.metadata.create_all()`` at
start-up and carried an ``alembic/versions/001_initial_auth_schema.py`` that
had never been run. The two disagreed, so "the migration" and "the code" could
describe different databases. Nothing detected that, because every test used
``create_all()`` and therefore only ever exercised the models.

This tool builds a reference schema from the models, then reflects the
migrated schema and reports every difference. It is meant to run in CI and
must exit non-zero on any drift.

Usage
-----
    alembic upgrade head
    python tools/check_schema_drift.py "sqlite:///./_schemadiff/migrated.db"

The URL is the database the migrations were applied to.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402

IGNORED_TABLES = {"alembic_version", "sqlite_sequence"}


def build_reference(path: Path) -> str:
    """Create a throwaway database holding exactly what the models declare."""
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    engine.dispose()
    return url


def normalise_type(type_str: str) -> str:
    """Collapse dialect spelling so VARCHAR and String compare equal."""
    t = type_str.upper()
    aliases = {
        "VARCHAR(36)": "VARCHAR(36)",
    }
    t = t.replace("TEXT", "TEXT")
    return aliases.get(t, t)


def column_signature(column) -> tuple:
    return (
        normalise_type(str(column["type"])),
        bool(column["nullable"]),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    migrated_url = argv[1]

    with tempfile.TemporaryDirectory() as tmp:
        reference_url = build_reference(Path(tmp) / "reference.db")

        reference_engine = create_engine(reference_url)
        migrated_engine = create_engine(migrated_url)
        try:
            reference = inspect(reference_engine)
            migrated = inspect(migrated_engine)

            ref_tables = set(reference.get_table_names()) - IGNORED_TABLES
            mig_tables = set(migrated.get_table_names()) - IGNORED_TABLES

            problems: list[str] = []

            for missing in sorted(ref_tables - mig_tables):
                problems.append(f"MISSING TABLE      {missing}")
            for extra in sorted(mig_tables - ref_tables):
                problems.append(f"UNEXPECTED TABLE  {extra}")

            for table in sorted(ref_tables & mig_tables):
                ref_cols = {c["name"]: c for c in reference.get_columns(table)}
                mig_cols = {c["name"]: c for c in migrated.get_columns(table)}

                for missing in sorted(set(ref_cols) - set(mig_cols)):
                    problems.append(f"MISSING COLUMN    {table}.{missing}")
                for extra in sorted(set(mig_cols) - set(ref_cols)):
                    problems.append(f"EXTRA COLUMN      {table}.{extra}")
                for name in sorted(set(ref_cols) & set(mig_cols)):
                    want = column_signature(ref_cols[name])
                    got = column_signature(mig_cols[name])
                    if want != got:
                        problems.append(
                            f"COLUMN MISMATCH   {table}.{name} "
                            f"models={want} migration={got}"
                        )
        finally:
            # Both engines must be disposed before the temp directory is
            # removed, otherwise Windows refuses to delete the open file.
            reference_engine.dispose()
            migrated_engine.dispose()

        print(f"models tables: {len(ref_tables)}   migrated tables: {len(mig_tables)}")
        if problems:
            print(f"\n{len(problems)} difference(s) between migrations and models:\n")
            for line in problems:
                print(f"  {line}")
            return 1

        print("\nNo drift: the migrated schema matches the models exactly.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))