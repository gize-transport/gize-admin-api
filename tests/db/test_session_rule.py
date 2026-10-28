"""GZ-007: the session rule. Every database use is inside `async with session.begin():`.

Autobegin is off, so a read outside a block fails at once instead of leaving a transaction
open that a later `session.begin()` would trip over.
"""

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import literal, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import create_engine, create_sessionmaker, get_session
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
