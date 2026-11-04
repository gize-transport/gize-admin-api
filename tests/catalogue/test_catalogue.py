"""GZ-036: what the rider app shows before a sign-up, without signing in.

`GET /v1/public/areas` lists the areas to pick from; `GET /v1/public/routes` the public
routes a rider could take between two of them: boarding at A or an area the route serves,
getting off at B. Only what a rider needs: never seat counts, launch progress or anything
staff-only. Both share the `PUBLIC_READ` limit, which fails open (read-only and cheap).
"""

import itertools
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.routes.models import AssignmentStatus, RouteArea, RouteStatus, RouteType
from tests.factories import (
    AreaFactory,
    CompanyFactory,
    CorridorFactory,
    RouteAssignmentFactory,
    RouteFactory,
)
from tests.staff.support import add_staff, signed_in
from tests.verification.support import broken_redis

AREAS, ROUTES = "/v1/public/areas", "/v1/public/routes"
PRICED = {"price_round_trip": 3000, "price_one_way": 1800}
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
_minutes = itertools.count()


def _later() -> datetime:
    """Rows made in one test share the transaction's clock: order them explicitly."""
    return START + timedelta(minutes=next(_minutes))


@dataclass(frozen=True)
class Trip:
    """Bole (A) to Mexico (B), passing Megenagna; Summit is elsewhere."""

    bole: uuid.UUID
    megenagna: uuid.UUID
    mexico: uuid.UUID
    summit: uuid.UUID
    corridor: uuid.UUID


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    async with db_session.begin():
        areas = [
            (await AreaFactory().create(db_session, name=name)).id
            for name in ("Bole", "Megenagna", "Mexico", "Summit")
        ]
        corridor = await CorridorFactory().create(
            db_session, origin_area_id=areas[0], destination_area_id=areas[2]
        )
    bole, megenagna, mexico, summit = areas
    return Trip(bole, megenagna, mexico, summit, corridor=corridor.id)


async def _route(
    session: AsyncSession,
    trip: Trip,
    *,
    serves: tuple[uuid.UUID, ...] = (),
    **values: Any,  # noqa: ANN401
) -> uuid.UUID:
    """A route on the trip's corridor, forming with prices unless told otherwise."""
    async with session.begin():
        route = await RouteFactory().create(
            session,
            **{
                "corridor_id": trip.corridor,
                "status": RouteStatus.FORMING,
                "created_at": _later(),
                **PRICED,
                **values,
            },
        )
        for position, area_id in enumerate(serves, start=1):
            session.add(RouteArea(route_id=route.id, area_id=area_id, position=position))
        return route.id


async def _seats(session: AsyncSession, route_id: uuid.UUID, count: int) -> None:
    async with session.begin():
        for _ in range(count):
            await RouteAssignmentFactory().create(session, route_id=route_id)


def _between(start: uuid.UUID, end: uuid.UUID, **params: str) -> dict[str, str]:
    return {"from_area_id": str(start), "to_area_id": str(end), **params}


async def _codes(api: httpx.AsyncClient, start: uuid.UUID, end: uuid.UUID) -> list[str]:
    response = await api.get(ROUTES, params=_between(start, end))
    assert response.status_code == 200, response.json()
    return [route["code"] for route in response.json()["items"]]


# Areas


async def test_anyone_lists_the_live_areas(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with db_session.begin():
        bole = await AreaFactory().create(db_session, name="Bole", created_at=_later())
        await AreaFactory().create(db_session, name="Gone", deleted_at=START, created_at=_later())
        cmc = await AreaFactory().create(db_session, name="CMC", created_at=_later())
    response = await api.get(AREAS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "items": [{"id": str(bole.id), "name": "Bole"}, {"id": str(cmc.id), "name": "CMC"}],
        "next_cursor": None,
    }


async def test_areas_are_found_by_how_their_name_starts(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with db_session.begin():
        for name in ("Bole", "Bole Bulbula", "Ayat", "Old Bole"):
            await AreaFactory().create(db_session, name=name, created_at=_later())
    response = await api.get(AREAS, params={"q": "bOLE"})
    assert [area["name"] for area in response.json()["items"]] == ["Bole", "Bole Bulbula"]


async def test_areas_come_in_pages(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    async with db_session.begin():
        for name in ("Ayat", "Bole", "CMC"):
            await AreaFactory().create(db_session, name=name, created_at=_later())
    names: list[str] = []
    cursor = None
    while True:
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(AREAS, params=params)).json()
        names += [area["name"] for area in body["items"]]
        if (cursor := body["next_cursor"]) is None:
            break
    assert names == ["Ayat", "Bole", "CMC"]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"q": "x" * 101}, "VALIDATION_FAILED"),
        ({"deleted": "true"}, "VALIDATION_FAILED"),
        ({"cursor": "not-a-cursor"}, "INVALID_CURSOR"),
    ],
    ids=str,
)
async def test_bad_area_parameters_are_422(
    api: httpx.AsyncClient, params: dict[str, str], code: str
) -> None:
    response = await api.get(AREAS, params=params)
    assert (response.status_code, response.json()["code"]) == (422, code)


# Routes


async def test_a_public_route_shows_what_a_rider_needs_and_nothing_else(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    route = await _route(
        db_session, trip, code="BOLE-MEX-01", serves=(trip.megenagna,), price_saturday=600
    )
    response = await api.get(ROUTES, params=_between(trip.bole, trip.mexico))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "items": [
            {
                "id": str(route),
                "code": "BOLE-MEX-01",
                "origin_area": {"id": str(trip.bole), "name": "Bole"},
                "destination_area": {"id": str(trip.mexico), "name": "Mexico"},
                "served_areas": [{"id": str(trip.megenagna), "name": "Megenagna"}],
                "morning_time": "06:30",
                "morning_arrival_time": "07:15",
                "return_time": "17:30",
                "return_arrival_time": "18:15",
                "prices": {"round_trip": 3000, "one_way": 1800, "saturday": 600},
                "status": "forming",
                "full": False,
            }
        ],
        "next_cursor": None,
    }


async def test_riders_board_at_a_or_an_area_served_and_get_off_at_b(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    await _route(db_session, trip, code="VIA-MEG", serves=(trip.megenagna,))
    await _route(db_session, trip, code="DIRECT")
    assert await _codes(api, trip.bole, trip.mexico) == ["VIA-MEG", "DIRECT"]
    assert await _codes(api, trip.megenagna, trip.mexico) == ["VIA-MEG"]
    assert await _codes(api, trip.mexico, trip.bole) == []  # the morning run goes A to B
    assert await _codes(api, trip.bole, trip.megenagna) == []  # B is where it ends
    assert await _codes(api, trip.summit, trip.mexico) == []


async def test_only_routes_a_rider_can_join_or_ask_for_are_listed(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """Forming (reserve a seat), active or paused (ask for one); public, priced, live."""
    for status in (RouteStatus.FORMING, RouteStatus.ACTIVE, RouteStatus.PAUSED):
        await _route(db_session, trip, code=f"PUB-{status.upper()}", status=status)
    await _route(db_session, trip, code="DRAFT", status=RouteStatus.DRAFT)
    await _route(db_session, trip, code="NO-PRICES", price_round_trip=None, price_one_way=None)
    await _route(db_session, trip, code="GONE", deleted_at=START)
    await _route(db_session, trip, code="SCHOOL", route_type=RouteType.STUDENT, school_name="ICS")
    async with db_session.begin():
        company = await CompanyFactory().create(db_session)
    await _route(
        db_session,
        trip,
        code="CORPORATE",
        route_type=RouteType.CORPORATE,
        company_id=company.id,
        status=RouteStatus.ACTIVE,
        launch_minimum=None,
        price_round_trip=None,
        price_one_way=None,
    )
    assert await _codes(api, trip.bole, trip.mexico) == ["PUB-FORMING", "PUB-ACTIVE", "PUB-PAUSED"]


async def test_a_route_is_full_without_a_free_seat_this_month_or_next(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """A flag, not counts: how many have signed up is for staff."""
    full = await _route(db_session, trip, code="FULL", seats=2, launch_minimum=2)
    await _seats(db_session, full, 2)
    one_left = await _route(db_session, trip, code="ONE-LEFT", seats=2, launch_minimum=2)
    await _seats(db_session, one_left, 1)
    running = await _route(
        db_session, trip, code="RUNNING", seats=1, launch_minimum=1, status=RouteStatus.ACTIVE
    )
    async with db_session.begin():
        await RouteAssignmentFactory().create(
            db_session,
            route_id=running,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),  # leaves at the month's end: free next month
            status=AssignmentStatus.ACTIVE,
        )
    response = await api.get(ROUTES, params=_between(trip.bole, trip.mexico))
    flags = {route["code"]: route["full"] for route in response.json()["items"]}
    assert flags == {"FULL": True, "ONE-LEFT": False, "RUNNING": True}


async def test_routes_come_in_pages(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    for code in ("R-A", "R-B", "R-C"):
        await _route(db_session, trip, code=code)
    first = (await api.get(ROUTES, params=_between(trip.bole, trip.mexico, limit="2"))).json()
    assert [route["code"] for route in first["items"]] == ["R-A", "R-B"]
    params = _between(trip.bole, trip.mexico, cursor=first["next_cursor"])
    second = (await api.get(ROUTES, params=params)).json()
    assert ([route["code"] for route in second["items"]], second["next_cursor"]) == (["R-C"], None)


async def test_areas_nobody_serves_list_no_routes(api: httpx.AsyncClient) -> None:
    assert await _codes(api, uuid.uuid4(), uuid.uuid4()) == []


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "VALIDATION_FAILED"),
        ({"from_area_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        ({"to_area_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        (_between(uuid.UUID(int=1), uuid.UUID(int=1)), "VALIDATION_FAILED"),  # nowhere to go
        ({"from_area_id": "bole", "to_area_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        (_between(uuid.UUID(int=1), uuid.UUID(int=2), limit="101"), "VALIDATION_FAILED"),
        (_between(uuid.UUID(int=1), uuid.UUID(int=2), route_type="student"), "VALIDATION_FAILED"),
        (_between(uuid.UUID(int=1), uuid.UUID(int=2), cursor="nope"), "INVALID_CURSOR"),
    ],
    ids=str,
)
async def test_bad_route_parameters_are_422(
    api: httpx.AsyncClient, params: dict[str, str], code: str
) -> None:
    response = await api.get(ROUTES, params=params)
    assert (response.status_code, response.json()["code"]) == (422, code)


# Who may call, limits, failures


@pytest.mark.parametrize("path", [AREAS, ROUTES])
async def test_no_sign_in_is_needed_and_staff_see_the_same(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, path: str
) -> None:
    await _route(db_session, trip)
    member = await add_staff(db_session)
    params = _between(trip.bole, trip.mexico) if path == ROUTES else {}
    anonymous = await api.get(path, params=params)
    staff = await api.get(path, params=params, headers=signed_in(member.id))
    assert (anonymous.status_code, staff.status_code) == (200, 200)
    assert anonymous.json() == staff.json()
    assert anonymous.json()["items"]


async def test_sixty_reads_a_minute_per_caller_across_both(
    api: httpx.AsyncClient, trip: Trip
) -> None:
    for n in range(60):
        path, params = (AREAS, {}) if n % 2 else (ROUTES, _between(trip.bole, trip.mexico))
        assert (await api.get(path, params=params)).status_code == 200
    response = await api.get(AREAS)
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")
    assert "Retry-After" in response.headers


@pytest.mark.parametrize("path", [AREAS, ROUTES])
async def test_without_redis_reads_still_answer(
    api: httpx.AsyncClient, api_app: GizeAPI, trip: Trip, path: str
) -> None:
    """Fails open: nothing is written or sent, and browsing shouldn't stop with Redis."""
    params = _between(trip.bole, trip.mexico) if path == ROUTES else {}
    async with broken_redis(api_app):
        response = await api.get(path, params=params)
    assert response.status_code == 200


@pytest.mark.parametrize("path", [AREAS, ROUTES])
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    path: str,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    params = _between(uuid.UUID(int=1), uuid.UUID(int=2)) if path == ROUTES else {}
    response = await api.get(path, params=params)
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
