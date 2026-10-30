"""GZ-021: route changes at the same time as each other, or as a corridor or company
change (committed rows)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.factories import CompanyFactory, CorridorFactory, RouteFactory
from tests.staff.support import add_admin, add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

BODY = {
    "code": "BOLE-01",
    "route_type": "public",
    "morning_time": "06:30",
    "return_time": "17:30",
    "seats": 14,
    "launch_minimum": 10,
}


# Fixed statements: no SQL is built from strings, even in tests.
LOCKS = {
    ("corridors", "UPDATE"): "SELECT 1 FROM gize.corridors WHERE id = $1 FOR UPDATE",
    ("corridors", "SHARE"): "SELECT 1 FROM gize.corridors WHERE id = $1 FOR SHARE",
    ("companies", "SHARE"): "SELECT 1 FROM gize.companies WHERE id = $1 FOR SHARE",
    ("routes", "UPDATE"): "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE",
}


@contextlib.asynccontextmanager
async def _holding(
    database: DatabaseUrls, table: str, row_id: uuid.UUID, mode: str = "UPDATE"
) -> AsyncIterator[asyncpg.Connection]:
    """A transaction holding a row lock, as a concurrent change would."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute(LOCKS[table, mode], row_id)
            yield conn
    finally:
        await conn.close()


async def test_two_creates_of_one_code_make_one_route(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            corridor = await CorridorFactory().create(session)
    body = {**BODY, "corridor_id": str(corridor.id)}
    responses = await asyncio.gather(
        *(committed_api.post("/v1/routes", json=body, headers=with_key(member.id)) for _ in "ab")
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"ROUTE_CODE_TAKEN"}


async def test_a_route_waiting_on_a_corridor_delete_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            corridor = await CorridorFactory().create(session)
    body = {**BODY, "corridor_id": str(corridor.id)}
    async with _holding(database, "corridors", corridor.id) as conn:
        request = asyncio.create_task(
            committed_api.post("/v1/routes", json=body, headers=with_key(member.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute(
            "UPDATE gize.corridors SET deleted_at = now() WHERE id = $1", corridor.id
        )
    response = await request
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "/corridor_id"


async def test_a_corridor_delete_waiting_on_a_new_route_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            corridor = await CorridorFactory().create(session)
    async with _holding(database, "corridors", corridor.id, mode="SHARE") as conn:
        request = asyncio.create_task(
            committed_api.delete(f"/v1/corridors/{corridor.id}", headers=signed_in(admin.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute(
            "INSERT INTO gize.routes (code, corridor_id, route_type, morning_time, return_time,"
            " seats, launch_minimum) VALUES ('RACE-01', $1, 'public', '06:30', '17:30', 14, 10)",
            corridor.id,
        )
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "CORRIDOR_IN_USE")


async def test_a_company_deactivation_waiting_on_a_route_launch_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The route becomes active while the deactivation waits for the company row."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            company = await CompanyFactory().create(session)
            route = await RouteFactory().create(
                session, route_type="corporate", company_id=company.id, launch_minimum=None
            )
    async with _holding(database, "companies", company.id, mode="SHARE") as conn:
        request = asyncio.create_task(
            committed_api.patch(
                f"/v1/companies/{company.id}",
                json={"is_active": False},
                headers=signed_in(admin.id),
            )
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.routes SET status = 'active' WHERE id = $1", route.id)
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "COMPANY_IN_USE")


async def test_a_seat_change_waiting_on_a_new_rider_counts_them(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """A rider added under the route's lock (as M3 does) counts once the change gets it."""
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session, seats=14, launch_minimum=1)
    async with _holding(database, "routes", route.id) as conn:
        request = asyncio.create_task(
            committed_api.patch(
                f"/v1/routes/{route.id}", json={"seats": 1}, headers=signed_in(member.id)
            )
        )
        await wait_for_lock_waiters(database)
        for _ in range(2):
            await conn.execute(
                "INSERT INTO gize.route_assignments (route_id, start_date, status)"
                " VALUES ($1, $2, 'reserved')",
                route.id,
                date(2027, 1, 1),
            )
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "SEATS_BELOW_TAKEN")
