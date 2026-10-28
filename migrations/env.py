"""Alembic environment: runs migrations as gize_migrator over asyncpg.

Settings come from MIGRATOR_DATABASE_URL, APP_ENV and DATABASE_SSL_ROOT_CERT, or from
`config.attributes` when called from code, as the tests do (see gize_api.core.migrations).
Tests may also pass `target_metadata` to compare against models of their own.
"""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection

from gize_api.core.config import ConfigError
from gize_api.core.migrations import (
    check_default_schema,
    create_migration_engine,
    include_name,
    load_migration_settings,
    lock_for_migrations,
)
from gize_api.core.models import SCHEMA
from gize_api.models import Base

config = context.config


def _run(connection: Connection) -> None:
    check_default_schema(connection.dialect.default_schema_name)
    context.configure(
        connection=connection,
        target_metadata=config.attributes.get("target_metadata", Base.metadata),
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    settings = load_migration_settings(config.attributes, os.environ)
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            await lock_for_migrations(connection)
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("migrations: offline (--sql) mode is not supported")
try:
    asyncio.run(_run_online())
except ConfigError as exc:
    raise SystemExit(f"migrations: {exc}") from None
