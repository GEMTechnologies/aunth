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

Why the reference is built on the target dialect
-----------------------------------------------
An earlier revision always built the reference on SQLite. That is only
sound when the target is also SQLite: comparing a SQLite-rendered reference
against PostgreSQL reports ``DATETIME`` vs ``TIMESTAMP`` for every
``DateTime`` column and drowns the real findings in dialect noise.

So the reference is now built *on the same dialect as the target*:

* SQLite target  -> reference built in a throwaway file.
* any other dialect -> reference built in a throwaway schema inside the same
  database, using SQLAlchemy's ``schema_translate_map``, then dropped.

Both paths compare like with like, so ``DateTime(timezone=True)`` vs
``TIMESTAMP WITHOUT TIME ZONE`` still reports as a genuine mismatch rather
than being normalised away.

Usage
-----
    alembic upgrade head
    python tools/check_schema_drift.py "sqlite:///./_schemadiff/migrated.db"
    python tools/check_schema_drift.py "postgresql+psycopg2://user:pw@host/granada_auth"

The URL is the database the migrations were applied to.
"""

from __future__ import annotations

import secrets
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

IGNORED_TABLES = {"alembic_version", "sqlite_sequence"}


def normalise_type(type_str: str) -> str:
    """Collapse dialect spelling that is genuinely equivalent.

    Only *spelling* differences of the same type are collapsed. A timezone-
    aware timestamp and a naive one are different types and must still
    compare unequal, so ``TIMESTAMP`` is never merged into
    ``TIMESTAMP WITH TIME ZONE``.
    """
    return " ".join(type_str.upper().split())


def column_signature(column) -> tuple:
    return (
        normalise_type(str(column["type"])),
        bool(column["nullable"]),
    )


def _create_schema(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    # ``schema_translate_map`` re-routes every unqualified model table into
    # the scratch schema, so the reference is built by the *target* dialect.
    models.Base.metadata.create_all(
        engine.execution_options(schema_translate_map={None: schema})
    )


def _drop_schema(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def compare(reference, ref_schema: str, migrated, mig_schema: str) -> list[str]:
    ref_tables = set(reference.get_table_names(schema=ref_schema)) - IGNORED_TABLES
    mig_tables = set(migrated.get_table_names(schema=mig_schema)) - IGNORED_TABLES

    problems: list[str] = []

    for missing in sorted(ref_tables - mig_tables):
        problems.append(f"MISSING TABLE      {missing}")
    for extra in sorted(mig_tables - ref_tables):
        problems.append(f"UNEXPECTED TABLE  {extra}")

    for table in sorted(ref_tables & mig_tables):
        ref_cols = {c["name"]: c for c in reference.get_columns(table, schema=ref_schema)}
        mig_cols = {c["name"]: c for c in migrated.get_columns(table, schema=mig_schema)}

        for missing in sorted(set(ref_cols) - set(mig_cols)):
            problems.append(f"MISSING COLUMN    {table}.{missing}")
        for extra in sorted(set(mig_cols) - set(ref_cols)):
            problems.append(f"EXTRA COLUMN      {table}.{extra}")
        for name in sorted(set(ref_cols) & set(mig_cols)):
            want = column_signature(ref_cols[name])
            got = column_signature(mig_cols[name])
            if want != got:
                problems.append(
                    f"COLUMN MISMATCH   {table}.{name} models={want} migration={got}"
                )

    return problems, len(ref_tables), len(mig_tables)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    migrated_url = argv[1]
    dialect = make_url(migrated_url).get_backend_name()

    if dialect == "sqlite":
        return _check_sqlite(migrated_url)
    return _check_same_database(migrated_url)


def _check_sqlite(migrated_url: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        reference_url = f"sqlite:///{(Path(tmp) / 'reference.db').as_posix()}"
        reference_engine = create_engine(reference_url)
        reference_engine.connect().close()
        models.Base.metadata.create_all(reference_engine)

        migrated_engine = create_engine(migrated_url)
        try:
            problems, ref_n, mig_n = compare(
                inspect(reference_engine), None, inspect(migrated_engine), None
            )
        finally:
            # Dispose before the temp directory is removed, otherwise Windows
            # refuses to delete the open file.
            reference_engine.dispose()
            migrated_engine.dispose()

    return _report("sqlite", ref_n, mig_n, problems)


def _check_same_database(migrated_url: str) -> int:
    engine = create_engine(migrated_url)
    scratch = f"_refcheck_{secrets.token_hex(6)}"
    try:
        _create_schema(engine, scratch)
        inspector = inspect(engine)
        target_schema = inspector.default_schema_name
        problems, ref_n, mig_n = compare(inspector, scratch, inspector, target_schema)
    finally:
        _drop_schema(engine, scratch)
        engine.dispose()

    return _report(engine.dialect.name, ref_n, mig_n, problems)


def _report(dialect: str, ref_n: int, mig_n: int, problems: list[str]) -> int:
    print(f"dialect: {dialect}")
    print(f"models tables: {ref_n}   migrated tables: {mig_n}")
    if problems:
        print(f"\n{len(problems)} difference(s) between migrations and models:\n")
        for line in problems:
            print(f"  {line}")
        return 1
    print("\nNo drift: the migrated schema matches the models exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))