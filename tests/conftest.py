"""Shared fixtures."""

import asyncio
import contextlib
import os
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.bootstrap import bootstrap
from gize_api.core.config import Settings
from gize_api.core.db import GizeSession, create_engine, create_sessionmaker, get_session
from gize_api.core.rls import row_level_security_statements
from gize_api.main import GizeAPI, create_app
from tests.support import (
    SETTINGS_ENV_VARS,
    TEST_SUPABASE_URL,
    UNREACHABLE_DATABASE_URL,
    UNREACHABLE_REDIS_URL,
    DatabaseUrls,
    SettingsFactory,
    database_urls,
    hold_server_lock,
    run_migrations,
    server_lock_path,
)

ROOT = Path(__file__).resolve().parent.parent


class ServiceUrls(BaseSettings):
    """Where the real test services live: the environment (CI) or the local `.env`."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    test_redis_url: str | None = None
    test_admin_database_url: str | None = None


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Build Settings from explicit values only, never from the developer's shell."""

    def factory(**overrides: Any) -> Settings:  # noqa: ANN401 - mirrors Settings' kwargs
        values: dict[str, Any] = {
            "app_env": "test",
            "redis_url": UNREACHABLE_REDIS_URL,
            "database_url": UNREACHABLE_DATABASE_URL,
            "supabase_url": TEST_SUPABASE_URL,
            "cors_allowed_origins": ("https://admin.example.com",),
            "log_level": "INFO",
            # Production refuses to start without Sentry/OTLP; tests opt in explicitly.
            "observability_required": False,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture(scope="session")
def test_redis_url() -> str:
    """A real Redis for tests (CI service or local compose). Never skipped: fails if unset."""
    url = ServiceUrls().test_redis_url
    if not url:
        pytest.fail("TEST_REDIS_URL is not set: add it to .env (see .env.example) or export it")
    return url


@pytest.fixture
def blackhole_url() -> Iterator[str]:
    """An http URL whose server accepts connections and never answers (a hung dependency)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)  # connections queue in the backlog; nothing ever reads them
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


def _admin_url() -> str:
    url = ServiceUrls().test_admin_database_url
    if not url:
        pytest.fail(
            "TEST_ADMIN_DATABASE_URL is not set: add it to .env (see .env.example) or export it"
        )
    return url


@pytest.fixture(scope="session")
def _test_server_lock() -> Iterator[None]:
    """One test run at a time per test server on this machine (worktrees share it).

    Roles, their memberships and settings are server-wide, so database tests of two runs
    would see each other's changes; a second run waits here until the first has finished.
    """
    with hold_server_lock(server_lock_path(_admin_url())):
        yield


async def _create_and_bootstrap(admin_url: str, urls: DatabaseUrls) -> None:
    # Database names are generated test constants; DDL cannot take bind parameters.
    conn = await asyncpg.connect(admin_url, timeout=3)
    try:
        await _refuse_a_development_server(conn, urls)
        await conn.execute(f'CREATE DATABASE "{urls.name}"')
        await bootstrap(urls.admin, urls.app, urls.migrator, create_supabase_roles=True)
    finally:
        await conn.close()


async def _refuse_a_development_server(admin: asyncpg.Connection, urls: DatabaseUrls) -> None:
    """Fail if gize_app exists with another password: that server is a developer's own."""
    if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'gize_app'"):
        return  # a fresh test server
    try:
        conn = await asyncpg.connect(urls.app.rsplit("/", 1)[0] + "/postgres", timeout=3)
    except asyncpg.exceptions.InvalidPasswordError:
        pytest.fail(
            "TEST_ADMIN_DATABASE_URL points at a server whose gize_app has another password,"
            " probably your development server. Use the compose test server (postgres-test)."
        )
    await conn.close()


async def _drop(admin_url: str, name: str) -> None:
    conn = await asyncpg.connect(admin_url, timeout=3)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@contextlib.contextmanager
def _prepared(prefix: str, *, migrate: bool) -> Iterator[DatabaseUrls]:
    """A new database with a per-run name, so concurrent runs never collide; dropped after."""
    admin = _admin_url()
    urls = database_urls(admin, f"{prefix}_{os.getpid()}_{secrets.token_hex(4)}")
    asyncio.run(_create_and_bootstrap(admin, urls))
    try:
        if migrate:
            run_migrations(urls.migrator, "head")
        yield urls
    finally:
        asyncio.run(_drop(admin, urls.name))


@pytest.fixture(scope="session")
def database(_test_server_lock: None) -> Iterator[DatabaseUrls]:
    """A fresh test database: bootstrapped like production and migrated to head."""
    with _prepared("gize_test", migrate=True) as urls:
        yield urls


@pytest.fixture
def scratch_database(_test_server_lock: None) -> Iterator[DatabaseUrls]:
    """A bootstrapped but unmigrated database for migration round trips."""
    with _prepared("gize_scratch", migrate=False) as urls:
        yield urls


@pytest.fixture
async def db_session(
    database: DatabaseUrls, make_settings: SettingsFactory
) -> AsyncIterator[AsyncSession]:
    """A session as the app role inside one outer transaction, rolled back after the test.

    The engine is the app's own (`create_engine`), so connections carry production's
    settings. Service code still calls `session.begin()`: with create_savepoint it runs as a
    savepoint. `now()` is constant inside the outer transaction, and rows are never visible
    to other connections: tests of row locking need committed data instead.
    """
    engine = create_engine(make_settings(database_url=database.app))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                autobegin=False,  # as in production (core/db.py, the session rule)
                sync_session_class=GizeSession,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def app_with_db_session(make_settings: SettingsFactory, db_session: AsyncSession) -> GizeAPI:
    """The app with `get_session` answering from `db_session` (rolled back after the test).

    Use it without the lifespan: the app's own engine is never created.
    """
    app = create_app(make_settings())

    async def override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = override
    return app


@pytest.fixture
async def committed_sessionmaker(
    database: DatabaseUrls, make_settings: SettingsFactory
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, for tests of concurrent requests on two connections.

    Every table in schema gize except the migration table is emptied afterwards (by the
    migrator: the app role cannot TRUNCATE).
    """
    engine = create_engine(make_settings(database_url=database.app))
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()
        migrator = await asyncpg.connect(database.migrator, timeout=3)
        try:
            tables = await migrator.fetch(
                "SELECT format('%I.%I', schemaname, tablename) AS name FROM pg_tables"
                " WHERE schemaname = 'gize' AND tablename <> 'alembic_version'"
            )
            if tables:
                names = ", ".join(row["name"] for row in tables)
                await migrator.execute(f"TRUNCATE {names}")  # names quoted by format('%I')
        finally:
            await migrator.close()


@pytest.fixture
async def widgets_table(database: DatabaseUrls) -> AsyncIterator[None]:
    """The table for the test-only Widget model (tests/db/widgets.py)."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await conn.execute(
            "CREATE TABLE gize.test_widgets ("
            " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            " label varchar(50) NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " deleted_at timestamptz)"
        )
        for statement in row_level_security_statements("test_widgets"):
            await conn.execute(statement)
        yield
    finally:
        await conn.execute("DROP TABLE IF EXISTS gize.test_widgets")
        await conn.close()
