"""GZ-026: two staff members creating or renaming to one name at once (committed rows).

The check before the insert can't see the other request's uncommitted row; the database's
unique index catches it, and the loser gets the same 409 as a sequential duplicate.
"""

import asyncio

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas.models import Area
from tests.factories import AreaFactory
from tests.staff.support import add_staff, signed_in, with_key


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
