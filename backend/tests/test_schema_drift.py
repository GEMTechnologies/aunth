"""The Alembic migrations must produce exactly the schema the models declare.

Before Phase 1 the two were wildly different: ``alembic.ini`` was a
placeholder file, ``alembic/env.py`` did not exist, and revision 001 covered 12
of 18 tables. Every database had therefore been built by
``Base.metadata.create_all()`` at start-up, so the migration history was
fiction and nothing noticed.

This test runs the real migration chain against a throwaway SQLite database and
compares the result against the models. It fails if a future model change is
made without a matching migration, which is the drift that made the original
schema/service mismatches possible in the first place.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parents[1]


def _import_alembic():
    """Import Alembic from ``conftest``, which owns the de-shadowing.

    The logic lives there rather than here because it has to run once for the
    whole process, before any test module is collected. An earlier version of
    this function repaired ``sys.modules`` only around its own import and then
    restored the saved shadow, which broke ``env.py``'s ``from alembic import
    context`` whenever this module ran after ``test_tenant_rls.py``.
    """
    from conftest import ALEMBIC_COMMAND, ALEMBIC_CONFIG

    return ALEMBIC_COMMAND, ALEMBIC_CONFIG


command, Config = _import_alembic()

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402

IGNORED_TABLES = {"alembic_version", "sqlite_sequence"}


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """Apply the full migration chain to a scratch SQLite database.

    BOTH URL variables are pinned, and that is not belt-and-braces. Alembic
    resolves its target in ``alembic/env.py::get_url()`` in this order:

    1. ``GRANADA_ADMIN_DATABASE_URL`` from ``os.environ``
    2. ``DATABASE_URL`` from ``os.environ``
    3. ``settings.database_url`` (the runtime role from ``.env``)

    Pinning only ``DATABASE_URL`` therefore loses to variable 1 whenever it is
    set - and the runbook instructs developers to export exactly that variable
    to run migrations by hand. A developer who followed the runbook and then ran
    pytest would have this fixture ``upgrade`` and then ``downgrade base`` their
    real database, because ``test_migrations_are_reversible`` rolls all the way
    back. That was a live hazard, not a hypothetical one: it was observed
    running against PostgreSQL before this pin was added.

    The assertion below makes the failure mode loud rather than silent: if a
    future change reintroduces another precedence path, this test must abort
    before touching anything.
    """
    db_path = tmp_path / "migrated.db"
    scratch_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", scratch_url)
    monkeypatch.setenv("GRANADA_ADMIN_DATABASE_URL", scratch_url)

    resolved = os.environ.get("GRANADA_ADMIN_DATABASE_URL", "")
    assert resolved.startswith("sqlite:///"), (
        f"migration target is not a scratch SQLite file: {resolved!r}. "
        "Refusing to run - this fixture downgrades to base."
    )
    assert os.environ.get("DATABASE_URL", "").startswith("sqlite:///")

    ini = BACKEND / "alembic.ini"
    assert ini.is_file(), "alembic.ini must exist"
    assert "// Placeholder" not in ini.read_text(encoding="utf-8"), (
        "alembic.ini still contains placeholder content"
    )

    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

    # A migration that silently targeted something else would leave no file
    # here, so this is the positive proof that SQLite was the target.
    assert db_path.is_file(), "alembic did not create the scratch database"

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def test_alembic_has_a_runnable_environment():
    """Alembic needs an env.py. Without it every command fails."""
    env_py = BACKEND / "alembic" / "env.py"
    assert env_py.is_file(), "alembic/env.py is missing; migrations cannot run"


def test_migrations_produce_exactly_the_model_schema(tmp_path, migrated_db):
    reference_url = f"sqlite:///{(tmp_path / 'reference.db').as_posix()}"
    reference_engine = create_engine(reference_url)
    try:
        models.Base.metadata.create_all(reference_engine)

        reference = inspect(reference_engine)
        migrated = inspect(migrated_db)

        ref_tables = set(reference.get_table_names()) - IGNORED_TABLES
        mig_tables = set(migrated.get_table_names()) - IGNORED_TABLES

        problems = [
            f"missing table {t}" for t in sorted(ref_tables - mig_tables)
        ] + [
            f"unexpected table {t}" for t in sorted(mig_tables - ref_tables)
        ]

        for table in sorted(ref_tables & mig_tables):
            ref_cols = {c["name"]: c for c in reference.get_columns(table)}
            mig_cols = {c["name"]: c for c in migrated.get_columns(table)}
            problems += [
                f"{table}: missing column {c}" for c in sorted(set(ref_cols) - set(mig_cols))
            ]
            problems += [
                f"{table}: extra column {c}" for c in sorted(set(mig_cols) - set(ref_cols))
            ]
            for name in sorted(set(ref_cols) & set(mig_cols)):
                if str(ref_cols[name]["type"]) != str(mig_cols[name]["type"]):
                    problems.append(
                        f"{table}.{name}: type {ref_cols[name]['type']} "
                        f"vs {mig_cols[name]['type']}"
                    )
                if ref_cols[name]["nullable"] != mig_cols[name]["nullable"]:
                    problems.append(
                        f"{table}.{name}: nullable {ref_cols[name]['nullable']} "
                        f"vs {mig_cols[name]['nullable']}"
                    )
    finally:
        reference_engine.dispose()

    assert not problems, "schema drift:\n  " + "\n  ".join(problems)


def test_migrations_are_reversible(migrated_db):
    """A migration you cannot roll back is not a migration."""
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.downgrade(cfg, "base")
    remaining = set(inspect(migrated_db).get_table_names()) - IGNORED_TABLES
    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"


def test_revisions_are_chained():
    """Each revision must declare the one it follows, or ordering is arbitrary."""
    import importlib.util

    versions = sorted((BACKEND / "alembic" / "versions").glob("0*.py"))
    assert versions, "no migration revisions found"

    seen = set()
    for path in versions:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.revision, f"{path.name} has no revision id"
        assert module.revision not in seen, f"duplicate revision id {module.revision}"
        seen.add(module.revision)
        if module.down_revision is None:
            assert module.revision == versions[0].stem, (
                f"{path.name} is the base revision but is not first"
            )

    head = Config(str(BACKEND / "alembic.ini"))
    head.set_main_option("script_location", str(BACKEND / "alembic"))
    script = command.ScriptDirectory.from_config(head)
    assert len(script.get_heads()) == 1, "the migration history has more than one head"