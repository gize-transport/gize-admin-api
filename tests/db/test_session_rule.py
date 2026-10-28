"""GZ-007: the session rule. Every database use is inside `async with session.begin():`.

Autobegin is off, so a read outside a block fails at once instead of leaving a transaction
open that a later `session.begin()` would trip over.
"""

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import String, literal, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.db import create_engine, create_sessionmaker, get_session
from gize_api.core.models import Base
from gize_api.core.rls import row_level_security_statements
from tests.support import DatabaseUrls, SettingsFactory


async def test_using_a_session_outside_a_transaction_block_fails(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    engine = create_engine(make_settings(database_url=database.app))
    try:
        async with create_sessionmaker(engine)() as session:
            with pytest.raises(InvalidRequestError, match="Autobegin is disabled"):
                await session.execute(select(literal(1)))
            async with session.begin():
                assert await session.scalar(select(literal(1))) == 1
            async with session.begin():  # one block per unit of work, one after another
                assert await session.scalar(select(literal(2))) == 2
    finally:
        await engine.dispose()


async def test_a_reading_dependency_then_a_service_in_one_request(
    app_with_db_session: FastAPI,
) -> None:
    """The pattern auth (GZ-010) will use: the dependency reads in its own short block."""

    async def current_user(session: AsyncSession = Depends(get_session)) -> str:  # noqa: B008
        async with session.begin():
            return str(await session.scalar(text("SELECT current_user")))

    @app_with_db_session.get("/_test/whoami")
    async def whoami(
        user: str = Depends(current_user),
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, str]:
        async with session.begin():  # the service's own unit of work
            database = str(await session.scalar(text("SELECT current_database()")))
        return {"user": user, "database": database}

    transport = httpx.ASGITransport(app=app_with_db_session)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/whoami")
    assert response.status_code == 200
    assert response.json()["user"] == "gize_app"


class _Probe(Base):
    __tablename__ = "test_rule_probe"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


@pytest.fixture
async def committed_probe(database: DatabaseUrls) -> AsyncIterator[None]:
    migrator = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await migrator.execute("CREATE TABLE gize.test_rule_probe (id int PRIMARY KEY, role text)")
        for statement in row_level_security_statements("test_rule_probe"):
            await migrator.execute(statement)
        await migrator.execute("INSERT INTO gize.test_rule_probe VALUES (1, 'admin')")
        yield
    finally:
        await migrator.execute("DROP TABLE IF EXISTS gize.test_rule_probe")
        await migrator.close()


@pytest.mark.usefixtures("committed_probe")
async def test_each_block_reads_current_rows_not_the_sessions_cache(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    """A dependency's block loads a row; another request changes it; the service's block in
    the same session must see the change (a revoked role must not pass a later check)."""
    engine = create_engine(make_settings(database_url=database.app))
    other = await asyncpg.connect(database.app, timeout=3)
    try:
        async with create_sessionmaker(engine)() as session:
            async with session.begin():
                first = await session.scalar(select(_Probe).where(_Probe.id == 1))
            assert first is not None
            assert first.role == "admin"
            await other.execute("UPDATE gize.test_rule_probe SET role = 'revoked' WHERE id = 1")
            async with session.begin():
                again = await session.scalar(select(_Probe).where(_Probe.id == 1).with_for_update())
            assert again is not None
            assert again.role == "revoked"
            assert first.role == "revoked"  # the same object, refreshed
    finally:
        await other.close()
        await engine.dispose()


@pytest.mark.usefixtures("committed_probe")
async def test_get_reads_current_rows_too(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    """`session.get()` answers from the session's cache without SQL unless told otherwise."""
    engine = create_engine(make_settings(database_url=database.app))
    other = await asyncpg.connect(database.app, timeout=3)
    try:
        async with create_sessionmaker(engine)() as session:
            async with session.begin():
                first = await session.get(_Probe, 1)
            await other.execute("UPDATE gize.test_rule_probe SET role = 'revoked' WHERE id = 1")
            async with session.begin():  # an explicit choice is respected
                cached = await session.get(_Probe, 1, populate_existing=False)
            assert cached is not None
            assert cached.role == "admin"
            async with session.begin():
                again = await session.get(_Probe, 1)
            assert again is first
            assert again is not None
            assert again.role == "revoked"
    finally:
        await other.close()
        await engine.dispose()


@pytest.mark.usefixtures("committed_probe")
async def test_refreshing_never_discards_unsaved_edits(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    """With autoflush off, a refresh would overwrite pending edits; then rows are not
    refreshed, and the edit is saved when the block ends."""
    engine = create_engine(make_settings(database_url=database.app))
    try:
        async with create_sessionmaker(engine)() as session:
            async with session.begin():
                probe = await session.get(_Probe, 1)
                assert probe is not None
                with session.no_autoflush:
                    probe.role = "edited"
                    await session.scalar(select(_Probe).where(_Probe.id == 1))
                    await session.get(_Probe, 1)
                    assert probe.role == "edited"
            async with session.begin():
                saved = await session.scalar(select(_Probe.role).where(_Probe.id == 1))
        assert saved == "edited"
    finally:
        await engine.dispose()
