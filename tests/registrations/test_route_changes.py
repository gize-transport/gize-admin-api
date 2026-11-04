"""GZ-032: route changes that would strand riders holding a seat are refused.

Staff can't drop an area where such a rider boards (their home area), and admins can't
clear the Saturday price while such a rider has the add-on: `409 ROUTE_HAS_RIDERS`.
"""

import uuid

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.registrations.support import (
    ADULT,
    Trip,
    add_route,
    add_trip,
    body,
    key,
    verified,
)
from tests.staff.support import add_admin, add_staff, signed_in

PRICES = {"round_trip": 3000, "one_way": 1800}


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


async def _sign_up(
    api: httpx.AsyncClient, session: AsyncSession, trip: Trip, route: uuid.UUID, **changes: object
) -> None:
    response = await api.post(
        ADULT, json=body(await verified(session), trip, route_id=str(route), **changes),
        headers=key(),
    )  # fmt: skip
    assert (response.status_code, response.json()["outcome"]) == (201, "reserved")


async def _end_seats(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(update(RouteAssignment).values(status=AssignmentStatus.ENDED))


async def test_an_area_a_rider_boards_in_stays_served(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    member = signed_in((await add_staff(db_session)).id)
    route = await add_route(db_session, trip, serves=(trip.megenagna, trip.summit))
    await _sign_up(api, db_session, trip, route, home_area_id=str(trip.megenagna))
    path = f"/v1/routes/{route}/areas"
    dropped = await api.put(path, json={"area_ids": [str(trip.summit)]}, headers=member)
    assert (dropped.status_code, dropped.json()["code"]) == (409, "ROUTE_HAS_RIDERS")
    kept = await api.put(path, json={"area_ids": [str(trip.megenagna)]}, headers=member)
    assert kept.status_code == 200  # nobody boards in Summit
    await _end_seats(db_session)
    assert (await api.put(path, json={"area_ids": []}, headers=member)).status_code == 200


async def test_the_saturday_price_stays_while_a_rider_has_the_add_on(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    admin = signed_in((await add_admin(db_session)).id)
    route = await add_route(db_session, trip, price_saturday=600)
    await _sign_up(api, db_session, trip, route, saturday=True)
    path = f"/v1/routes/{route}/prices"
    cleared = await api.put(path, json=PRICES, headers=admin)
    assert (cleared.status_code, cleared.json()["code"]) == (409, "ROUTE_HAS_RIDERS")
    changed = await api.put(path, json={**PRICES, "saturday": 700}, headers=admin)
    assert changed.status_code == 200  # a new price is billing's concern, not a strand
    await _end_seats(db_session)
    assert (await api.put(path, json=PRICES, headers=admin)).status_code == 200


async def test_riders_without_the_add_on_let_it_go(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    admin = signed_in((await add_admin(db_session)).id)
    route = await add_route(db_session, trip, price_saturday=600)
    await _sign_up(api, db_session, trip, route)
    response = await api.put(f"/v1/routes/{route}/prices", json=PRICES, headers=admin)
    assert response.status_code == 200
