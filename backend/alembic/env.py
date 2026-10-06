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
    """Resolve the database URL.

    An explicit DATABASE_URL in the environment wins over application
    settings, so tooling and tests can target a scratch database without
    editing the committed configuration. Otherwise the service's own setting
    is used, which keeps migrations pointed at the same database the service
    talks to and keeps credentials out of this repository.
    """
    import os

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