"""Alembic environment: runs migrations as gize_migrator over asyncpg.

The URL comes from MIGRATOR_DATABASE_URL (or `config.attributes["database_url"]` when called
from code, as the tests do). The app's settings are not loaded: migrating needs no API secrets.
"""

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from gize_api.core.bootstrap import MIGRATOR_ROLE, role_from_url
from gize_api.core.db import async_url, ssl_connect_argument
from gize_api.core.models import SCHEMA, Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("MIGRATOR_DATABASE_URL")
    if not url:
        raise SystemExit("migrations: set MIGRATOR_DATABASE_URL")
    if role_from_url(url) != MIGRATOR_ROLE:
        raise SystemExit(f"migrations: MIGRATOR_DATABASE_URL must connect as {MIGRATOR_ROLE}")
    return str(url)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url = _url()
    engine = create_async_engine(
        async_url(url),
        poolclass=pool.NullPool,
        connect_args={
            "timeout": 10,
            "server_settings": {"application_name": "gize-migrations", "lock_timeout": "5000"},
            **ssl_connect_argument(url),
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("migrations: offline (--sql) mode is not supported")
asyncio.run(_run_online())
