"""GZ-020: corridor changes running at the same time as each other or as an area delete
(committed rows)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas.models import Area
from gize_api.corridors.models import Corridor
from tests.factories import AreaFactory
from tests.staff.support import add_admin, add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters


@contextlib.asynccontextmanager
async def _holding_area(
    database: DatabaseUrls, area_id: uuid.UUID
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction holding the area's row lock, as an admin deleting it would."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE", area_id)
            yield conn
    finally:
        await conn.close()


async def test_a_corridor_waiting_on_an_area_delete_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The delete commits first: the corridor would point at a deleted area, so it's 422."""
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            ayat, mexico = await AreaFactory().create_batch(session, 2)
    body = {"origin_area_id": str(ayat.id), "destination_area_id": str(mexico.id)}
    async with _holding_area(database, ayat.id) as conn:
        request = asyncio.create_task(
            committed_api.post("/v1/corridors", json=body, headers=with_key(member.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.areas SET deleted_at = now() WHERE id = $1", ayat.id)
    response = await request
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "/origin_area_id"
    async with committed_sessionmaker() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Corridor)) == 0


async def test_an_area_delete_waiting_on_a_new_corridor_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The corridor commits first: the area is now in use, so the delete is 409."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            ayat, mexico = await AreaFactory().create_batch(session, 2)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.areas WHERE id = $1 FOR SHARE", ayat.id)
            request = asyncio.create_task(
                committed_api.delete(f"/v1/areas/{ayat.id}", headers=signed_in(admin.id))
            )
            await wait_for_lock_waiters(database)
            await conn.execute(
                "INSERT INTO gize.corridors (origin_area_id, destination_area_id) VALUES ($1, $2)",
                ayat.id,
                mexico.id,
            )
    finally:
        await conn.close()
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")
    async with committed_sessionmaker() as session, session.begin():
        assert (await session.get_one(Area, ayat.id)).deleted_at is None


async def test_two_creates_of_one_pair_make_one_corridor(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            ayat, mexico = await AreaFactory().create_batch(session, 2)
    body = {"origin_area_id": str(ayat.id), "destination_area_id": str(mexico.id)}
    responses = await asyncio.gather(
        *(committed_api.post("/v1/corridors", json=body, headers=with_key(member.id)) for _ in "ab")
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"CORRIDOR_EXISTS"}
