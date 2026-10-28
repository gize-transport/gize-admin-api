"""Settings and helpers for the migration run (migrations/env.py), kept here to be tested.

Migrations run as gize_migrator over MIGRATOR_DATABASE_URL, under the app's URL and TLS
rules (APP_ENV, DATABASE_SSL_ROOT_CERT) but without its API settings: migrating needs no
API secrets. Use a direct or session-mode connection: the run holds a session lock.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from gize_api.core.bootstrap import MIGRATOR_ROLE, role_from_url
from gize_api.core.config import (
    ConfigError,
    Environment,
    database_ssl_mode,
    database_tls_problem,
    is_database_url,
)
from gize_api.core.db import async_url, ssl_connect_argument
from gize_api.core.models import SCHEMA

# Held for the whole run, so two release steps never migrate at once.
MIGRATION_LOCK_ID = 0x67697A656D6967  # "gizemig"
CONNECT_TIMEOUT_SECONDS = 10
LOCK_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class MigrationSettings:
    url: str
    ssl_root_cert: Path | None


def load_migration_settings(
    attributes: Mapping[str, object], environ: Mapping[str, str]
) -> MigrationSettings:
    """Read and check the settings; `attributes` (from code, as in tests) win over `environ`.

    Raises ConfigError with a message that never contains the URL.
    """
    url = str(attributes.get("database_url") or environ.get("MIGRATOR_DATABASE_URL") or "")
    if not url:
        raise ConfigError("set MIGRATOR_DATABASE_URL")
    if role_from_url(url) != MIGRATOR_ROLE:
        raise ConfigError(f"MIGRATOR_DATABASE_URL must connect as {MIGRATOR_ROLE}")
    if not is_database_url(url):
        raise ConfigError(
            "MIGRATOR_DATABASE_URL must be"
            " postgresql://user:password@host[:port]/database[?ssl=require|verify-full]"
        )
    try:
        app_env = Environment(str(attributes.get("app_env") or environ.get("APP_ENV") or ""))
    except ValueError:
        raise ConfigError("set APP_ENV to local, test, staging or production") from None
    root = attributes.get("ssl_root_cert") or environ.get("DATABASE_SSL_ROOT_CERT")
    root_cert = Path(str(root)) if root else None
    if root_cert is not None and not root_cert.is_file():
        raise ConfigError("DATABASE_SSL_ROOT_CERT must be a readable file")
    problem = database_tls_problem(app_env, database_ssl_mode(url), root_cert=root_cert is not None)
    if problem is not None:
        raise ConfigError(f"MIGRATOR_DATABASE_URL {problem}")
    return MigrationSettings(url=url, ssl_root_cert=root_cert)


def create_migration_engine(settings: MigrationSettings) -> AsyncEngine:
    """One connection, no pool; bound parameters never appear in error text."""
    return create_async_engine(
        async_url(settings.url),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "timeout": CONNECT_TIMEOUT_SECONDS,
            "server_settings": {
                "application_name": "gize-migrations",
                "lock_timeout": str(LOCK_TIMEOUT_MS),
            },
            **ssl_connect_argument(settings.url, settings.ssl_root_cert),
        },
    )


async def lock_for_migrations(connection: AsyncConnection) -> None:
    """Take the run's session lock, or raise ConfigError if another run holds it.

    The lock is released when the connection closes (the engine has no pool).
    """
    locked = await connection.scalar(select(func.pg_try_advisory_lock(MIGRATION_LOCK_ID)))
    await connection.commit()  # migrations then run in their own transactions
    if not locked:
        raise ConfigError("another migration is running; try again when it has finished")


def check_default_schema(default_schema: str | None) -> None:
    """Alembic names the connection's default schema None, so `gize` must not be it."""
    if default_schema == SCHEMA:
        raise ConfigError(f"{MIGRATOR_ROLE}'s search_path must not start with {SCHEMA}")


def include_name(
    name: str | None, type_: NameFilterType, parent_names: NameFilterParentNames
) -> bool:
    """Autogenerate and `alembic check` compare schema `gize` only, never Supabase's."""
    return type_ != "schema" or name == SCHEMA
