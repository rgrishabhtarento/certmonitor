"""Alembic environment.

Runs migrations with a synchronous psycopg2 connection even though the
application uses asyncpg: migrations are a one-shot administrative task and a
sync driver keeps this file simple and easy to run from a shell.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the application package importable when alembic is invoked from the
# backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.models import Base  # noqa: E402  (registers every mapped class)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The URL always comes from the environment, never from alembic.ini.
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    """Keep autogenerate focused on our own objects."""
    if type_ == "table" and name in {"spatial_ref_sys"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it.

    Useful where a DBA applies changes by hand:
    ``alembic upgrade head --sql > migration.sql``
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            # One transaction for the whole upgrade: a failure part-way
            # through leaves the schema untouched rather than half-migrated.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
