"""GZ-034: rider changes at the same time as each other or as a route change (committed
rows, each request its own connection). Each test holds the contended row lock until the
requests wait on it, so they really overlap."""

import asyncio
import uuid
from datetime import date

import asyncpg
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.riders.models import Rider
from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.factories import RiderFactory
from tests.registrations.support import add_route, add_trip
from tests.staff.support import add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

RIDERS = "/v1/riders"

# Fixed statements: no SQL is built from strings, even in tests.
LOCK_AREA = "SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE"
LOCK_ROUTE = "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE"
DROP_SERVED = "DELETE FROM gize.route_areas WHERE route_id = $1 AND area_id = $2"
END_SEATS = "UPDATE gize.route_assignments SET status = 'ended' WHERE route_id = $1"


async def test_two_records_of_one_phone_make_one_rider(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Released together: one rider, the other a 409 RIDER_EXISTS, whether the check or
    the unique index catches it (test_rules.py proves the index's answer directly)."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        trip = await add_trip(session)
    body = {
        "full_name": "Abebe Kebede",
        "phone": "+251911234567",
        "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico),
        "plan": "round_trip",
    }
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute(LOCK_AREA, max(trip.bole, trip.mexico))  # each locks it last
        tasks = [
            asyncio.create_task(committed_api.post(RIDERS, json=body, headers=with_key(member)))
            for _ in "ab"
        ]
        await wait_for_lock_waiters(database, count=2)
        await held.rollback()
        responses = await asyncio.gather(*tasks)
    finally:
        await conn.close()
    assert sorted(response.status_code for response in responses) == [201, 409]
    (refused,) = [response for response in responses if response.status_code == 409]
    assert refused.json()["code"] == "RIDER_EXISTS"
    async with committed_sessionmaker() as session, session.begin():
        assert len((await session.scalars(select(Rider))).all()) == 1


async def test_an_edit_waiting_on_a_route_change_judges_the_new_route(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Staff drop Megenagna from the route while another moves a rider to board there:
    the edit waits for the route, then finds Megenagna no longer served (422)."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        trip = await add_trip(session)
        route = await add_route(session, trip, serves=(trip.megenagna,))
        async with session.begin():
            rider = await RiderFactory().create(
                session, home_area_id=trip.bole, destination_area_id=trip.mexico
            )
            rider_id: uuid.UUID = rider.id
            session.add(
                RouteAssignment(
                    route_id=route,
                    rider_id=rider_id,
                    start_date=date(2026, 12, 1),
                    status=AssignmentStatus.RESERVED,
                )
            )
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        change = conn.transaction()
        await change.start()
        await conn.execute(LOCK_ROUTE, route)
        await conn.execute(DROP_SERVED, route, trip.megenagna)
        edit = asyncio.create_task(
            committed_api.patch(
                f"{RIDERS}/{rider_id}",
                json={"home_area_id": str(trip.megenagna)},
                headers=signed_in(member),
            )
        )
        await wait_for_lock_waiters(database)
        await change.commit()
        response = await edit
    finally:
        await conn.close()
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/home_area_id"]


async def test_an_edit_waiting_on_a_launch_that_lapses_the_seat_is_free(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The seat ends (a launch lapses it) while the edit waits for the route: after the
    wait the rider holds nothing, so the route no longer limits the edit."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        trip = await add_trip(session)
        route = await add_route(session, trip)
        async with session.begin():
            rider = await RiderFactory().create(
                session, home_area_id=trip.bole, destination_area_id=trip.mexico
            )
            rider_id: uuid.UUID = rider.id
            session.add(
                RouteAssignment(
                    route_id=route,
                    rider_id=rider_id,
                    start_date=date(2026, 12, 1),
                    status=AssignmentStatus.RESERVED,
                )
            )
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        launch = conn.transaction()
        await launch.start()
        await conn.execute(LOCK_ROUTE, route)
        await conn.execute(END_SEATS, route)
        edit = asyncio.create_task(
            committed_api.patch(
                f"{RIDERS}/{rider_id}",
                json={"home_area_id": str(trip.summit)},  # not on the route
                headers=signed_in(member),
            )
        )
        await wait_for_lock_waiters(database)
        await launch.commit()
        response = await edit
    finally:
        await conn.close()
    assert response.status_code == 200, response.json()
