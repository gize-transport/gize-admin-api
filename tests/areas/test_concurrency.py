"""GZ-026: two staff members creating or renaming to one name at once (committed rows).

The check before the insert can't see the other request's uncommitted row; the database's
unique index catches it, and the loser gets the same 409 as a sequential duplicate.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas.models import Area
from gize_api.core.audit import AuditEntry
from tests.factories import AreaFactory
from tests.staff.support import add_admin, add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters


async def test_two_creates_of_one_name_make_one_area(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
    responses = await asyncio.gather(
        committed_api.post("/v1/areas", json={"name": "Ayat"}, headers=with_key(member.id)),
        committed_api.post("/v1/areas", json={"name": "AYAT"}, headers=with_key(member.id)),
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert {r.json().get("code") for r in responses if r.status_code == 409} == {"AREA_NAME_TAKEN"}
    async with committed_sessionmaker() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Area)) == 1


async def test_two_renames_to_one_name_leave_one(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            first, second = await AreaFactory().create_batch(session, 2)
    responses = await asyncio.gather(
        *(
            committed_api.patch(
                f"/v1/areas/{area.id}", json={"name": "Bole"}, headers=signed_in(member.id)
            )
            for area in (first, second)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"AREA_NAME_TAKEN"}
    async with committed_sessionmaker() as session, session.begin():
        named = select(func.count()).where(Area.name == "Bole").select_from(Area)
        assert await session.scalar(named) == 1


@contextlib.asynccontextmanager
async def _holding_the_row(
    database: DatabaseUrls, area_id: uuid.UUID
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction holding the area's row lock (as a concurrent change would)."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE", area_id)
            yield conn
    finally:
        await conn.close()


@pytest.mark.parametrize(("method", "body"), [("PATCH", {"name": "Bole"}), ("DELETE", None)])
async def test_a_change_waiting_on_a_delete_finds_the_area_gone(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    method: str,
    body: object,
) -> None:
    """The delete commits while the change waits for the row: the change gets 404 and
    neither renames a deleted area nor deletes it twice."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            area = await AreaFactory().create(session, name="Ayat")
    async with _holding_the_row(database, area.id) as conn:
        request = asyncio.create_task(
            committed_api.request(
                method, f"/v1/areas/{area.id}", json=body, headers=signed_in(admin.id)
            )
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.areas SET deleted_at = now() WHERE id = $1", area.id)
    response = await request
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")
    async with committed_sessionmaker() as session, session.begin():
        row = await session.get_one(Area, area.id)
        assert row.name == "Ayat"
        entries = await session.scalars(select(AuditEntry).where(AuditEntry.entity_id == area.id))
        assert list(entries) == []
