"""Alembic runtime environment for granada-auth.

This file was missing entirely, so ``alembic upgrade head`` could never run
and the schema could only be produced by ``create_all()`` at start-up.

The URL is taken from application settings rather than alembic.ini, so
migrations target the same database the service uses and no credential is
committed.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the backend package importable regardless of the caller's cwd.
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = models.Base.metadata


def get_url() -> str:
    """Resolve the migration (owner) database URL.

    Three sources, most specific first:

    1. ``GRANADA_ADMIN_DATABASE_URL`` - the schema owner's credentials.
    2. ``DATABASE_URL`` - an explicit override, kept for tooling and tests that
       target a scratch database without editing committed configuration.
    3. ``config.settings.database_url`` - the service's own setting.

    ``GRANADA_ADMIN_DATABASE_URL`` exists because ``DATABASE_URL`` now names the
    *runtime* role (``granada_app``), which deliberately cannot create objects.
    Migrations need the owner: they create tables, and several of them enable
    row-level security, which the owner needs in order to seed reference data
    before policies are switched on. Running Alembic against ``DATABASE_URL``
    would now fail, and the previous fallback - silently using the runtime role -
    was the kind of ambiguity that invites running a migration against the
    wrong database.

    Falls back to an empty string, which Alembic reports as a configuration
    error, rather than guessing a database name.
    """
    import os

    admin = os.environ.get("GRANADA_ADMIN_DATABASE_URL")
    if admin:
        return admin

    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    try:
        from config import settings

        url = getattr(settings, "database_url", None)
        if url:
            return url
    except Exception:  # noqa: BLE001 - settings must never block migrations
        pass

    return ""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        # SQLite cannot ALTER a constraint, and revision 001 adds
        # users.primary_email_id as a foreign key after the table exists.
        # Batch mode rewrites that as copy-and-move.
        render_as_batch=_is_sqlite(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    section = config.get_section(config.config_ini_section) or {}
    url = get_url()
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=_is_sqlite(url),
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()