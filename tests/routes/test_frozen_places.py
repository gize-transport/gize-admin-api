"""Owner decision (2026-10-31, after M2): a route's corridor and school are frozen once it
has prices or open route requests. Prices were set for that corridor and school, and
requests asked for it as it is; to serve somewhere else, staff add a new route. Other
changes (code, times, seats) stay open. Riders already freeze them (`ROUTE_HAS_RIDERS`).
"""

import asyncio
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.main import create_app
from tests.factories import RouteFactory
from tests.route_requests.support import add_request
from tests.routes.support import add_corridor, add_route, route_path
from tests.staff.support import add_admin, add_staff, signed_in
from tests.support import DatabaseUrls, SettingsFactory, wait_for_lock_waiters

PRICES = {"price_round_trip": 2400, "price_one_way": 1400}


async def _moves(session: AsyncSession) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """(route values, a change that moves it) for a public and a student route."""
    other_corridor = await add_corridor(session)
    return [
        ({}, {"corridor_id": str(other_corridor)}),
        ({"route_type": "student", "school_name": "ICS"}, {"school_name": "Sandford"}),
    ]


async def test_a_priced_route_keeps_its_corridor_and_school(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Admins too: the prices were set for this corridor and school."""
    admin_id = (await add_admin(db_session)).id
    for values, change in await _moves(db_session):
        route = await add_route(db_session, **values, **PRICES)
        response = await api.patch(route_path(route), json=change, headers=signed_in(admin_id))
        assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_PRICES"), change


@pytest.mark.parametrize("named_as", ["asked for", "matched to"])
async def test_a_route_open_requests_name_keeps_its_corridor_and_school(
    api: httpx.AsyncClient, db_session: AsyncSession, named_as: str
) -> None:
    member_id = (await add_staff(db_session)).id
    for values, change in await _moves(db_session):
        route = await add_route(db_session, **values, status="active")
        student = {"rider_type": "student", "school_name": "ICS"} if values else {}
        if named_as == "asked for":
            await add_request(
                db_session, kind="route", route_id=route, from_area_name=None,
                to_area_name=None, **student,
            )  # fmt: skip
        else:
            await add_request(db_session, status="matched", matched_route_id=route, **student)
        response = await api.patch(route_path(route), json=change, headers=signed_in(member_id))
        assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_REQUESTS")


async def test_closed_requests_dont_freeze_a_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    for values, change in await _moves(db_session):
        route = await add_route(db_session, **values)
        student = {"rider_type": "student", "school_name": "ICS"} if values else {}
        await add_request(
            db_session, status="declined", decline_reason="Too far.", matched_route_id=route,
            **student,
        )  # fmt: skip
        response = await api.patch(route_path(route), json=change, headers=signed_in(member_id))
        assert response.status_code == 200, change


async def test_a_frozen_route_still_changes_everything_else(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, route_type="student", school_name="ICS", **PRICES)
    await add_request(
        db_session, status="matched", matched_route_id=route, rider_type="student",
        school_name="ICS",
    )  # fmt: skip
    change = {"code": "ICS-02", "morning_time": "06:45", "seats": 16, "school_name": "ICS"}
    response = await api.patch(route_path(route), json=change, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (200, "ICS-02")


async def test_a_move_waiting_on_a_new_request_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """A request share-locks the route it names; the change waits for it, then sees it."""
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session, status="active")
        other = await add_corridor(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.routes WHERE id = $1 FOR SHARE", route.id)
            request = asyncio.create_task(
                committed_api.patch(
                    route_path(route.id),
                    json={"corridor_id": str(other)},
                    headers=signed_in(member.id),
                )
            )
            await wait_for_lock_waiters(database)
            await conn.execute(
                "INSERT INTO gize.route_requests (kind, route_id, rider_type, rider_name,"
                " contact_name, contact_phone) VALUES ('route', $1, 'adult', 'Abebe', 'Abebe',"
                " '+251911234567')",
                route.id,
            )
    finally:
        await conn.close()
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_REQUESTS")


def test_the_freeze_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"]["/v1/routes/{route_id}"]["patch"]
    codes = operation["responses"]["409"]["x-error-codes"]
    assert {"ROUTE_HAS_PRICES", "ROUTE_HAS_REQUESTS"} <= set(codes)
