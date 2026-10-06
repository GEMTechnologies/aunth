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

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parents[1]


def _import_alembic():
    """Import the Alembic distribution, not the local ./alembic directory.

    ``Auth/backend/alembic/`` is the migration script directory, but it is also
    a package, so whenever the backend root is on ``sys.path`` it shadows the
    installed ``alembic`` distribution and ``from alembic import command``
    fails. That is a real latent hazard in this layout; here we hide the
    backend root while Alembic is imported rather than renaming the directory,
    which would break every documented ``alembic`` invocation.
    """
    backend_resolved = BACKEND.resolve()
    saved_path = list(sys.path)
    sys.path[:] = [
        p for p in sys.path
        if not (p and Path(p).resolve() == backend_resolved)
    ]
    saved_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "alembic" or name.startswith("alembic.")
    }
    for name in saved_modules:
        del sys.modules[name]
    try:
        from alembic import command
        from alembic.config import Config
    finally:
        sys.path[:] = saved_path
        sys.modules.update(saved_modules)
    return command, Config


command, Config = _import_alembic()

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402

IGNORED_TABLES = {"alembic_version", "sqlite_sequence"}


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """Apply the full migration chain to a scratch SQLite database."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    ini = BACKEND / "alembic.ini"
    assert ini.is_file(), "alembic.ini must exist"
    assert "// Placeholder" not in ini.read_text(encoding="utf-8"), (
        "alembic.ini still contains placeholder content"
    )

    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

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