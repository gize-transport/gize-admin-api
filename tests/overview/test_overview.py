"""GZ-064: the admin home page's summary ("today" is 15 November 2026).

`GET /v1/overview` (staff): counts only, each one query, whatever the data's size. Live
(not deleted) rows only. Driver applications awaiting review went with GZ-052 (deferred):
active routes without a driver take their place.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import schedule
from gize_api.core.db import get_session
from gize_api.drivers.models import Driver
from gize_api.main import GizeAPI
from gize_api.offers.models import OfferStatus, SeatOffer
from gize_api.route_requests.models import RequestStatus
from gize_api.routes.models import AssignmentStatus, RouteAssignment, RouteStatus
from tests.factories import RiderFactory, RouteRequestFactory
from tests.routes.support import add_route
from tests.staff.support import add_staff, signed_in

OVERVIEW = "/v1/overview"


async def _seat(session: AsyncSession, route: uuid.UUID, **values: object) -> uuid.UUID:
    async with session.begin():
        rider = await RiderFactory().create(session)
        session.add(
            RouteAssignment(
                **{
                    "route_id": route,
                    "rider_id": rider.id,
                    "start_date": date(2026, 12, 1),
                    "status": AssignmentStatus.RESERVED,
                    **values,
                }
            )
        )
        return rider.id


async def test_an_empty_system_counts_zero(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    response = await api.get(OVERVIEW, headers=signed_in(member))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "as_of": "2026-11-15",
        "routes": {
            "draft": 0, "forming": 0, "active": 0, "paused": 0,
            "ready_to_launch": 0, "active_without_driver": 0,
        },
        "riders": {"total": 0, "riding": 0, "reserved": 0},
        "route_requests": {"open": 0},
        "seat_offers": {"open": 0},
        "drivers": {"active": 0},
    }  # fmt: skip


async def test_the_overview_counts_what_staff_act_on(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    await add_route(db_session, status=RouteStatus.DRAFT)
    await add_route(db_session, status=RouteStatus.PAUSED)
    await add_route(db_session, status=RouteStatus.ACTIVE, deleted_at=datetime.now(UTC))
    # Two forming routes: one has its minimum reserved for 1 December, one hasn't.
    ready = await add_route(db_session, status=RouteStatus.FORMING, launch_minimum=2)
    await _seat(db_session, ready)
    await _seat(db_session, ready)
    short = await add_route(db_session, status=RouteStatus.FORMING, launch_minimum=2)
    await _seat(db_session, short)
    await _seat(db_session, short, status=AssignmentStatus.ENDED)  # left: not counted
    # Two active routes, one with a driver; one rider riding, one who left.
    async with db_session.begin():
        driver = Driver(full_name="T", phone="+251911000001", address="Bole", plate="AA 1-1")
        db_session.add(driver)
        db_session.add(Driver(full_name="I", phone="+251911000002", address="CMC",
                              plate="AA 2-2", is_active=False))  # fmt: skip
        await db_session.flush()
        driver_id = driver.id
    driven = await add_route(db_session, status=RouteStatus.ACTIVE, driver_id=driver_id)
    await add_route(db_session, status=RouteStatus.ACTIVE)
    await _seat(db_session, driven, status=AssignmentStatus.ACTIVE, start_date=date(2026, 11, 1))
    await _seat(db_session, driven, status=AssignmentStatus.ACTIVE,
                start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))  # fmt: skip
    async with db_session.begin():
        await RiderFactory().create(db_session, deleted_at=datetime.now(UTC))
        open_request = await RouteRequestFactory().create(  # waiting for `driven`
            db_session, kind="route", route_id=driven, from_area_name=None, to_area_name=None
        )
        await RouteRequestFactory().create(
            db_session, status=RequestStatus.MATCHED, matched_route_id=ready
        )
        await RouteRequestFactory().create(
            db_session, status=RequestStatus.DECLINED, decline_reason="Moved."
        )
        await RouteRequestFactory().create(db_session, deleted_at=datetime.now(UTC))
        now = datetime.now(UTC)
        for expires in (now + timedelta(hours=1), now - timedelta(hours=1)):  # one lapsed
            db_session.add(SeatOffer(route_id=driven, request_id=open_request.id,
                                     start_date=date(2026, 12, 1), expires_at=expires,
                                     status=OfferStatus.OPEN if expires > now
                                     else OfferStatus.EXPIRED))  # fmt: skip
    response = await api.get(OVERVIEW, headers=signed_in(member))
    assert response.json() == {
        "as_of": "2026-11-15",
        "routes": {
            "draft": 1, "forming": 2, "active": 2, "paused": 1,
            "ready_to_launch": 1, "active_without_driver": 1,
        },
        "riders": {"total": 6, "riding": 1, "reserved": 3},
        "route_requests": {"open": 2},
        "seat_offers": {"open": 1},
        "drivers": {"active": 1},
    }  # fmt: skip


async def test_signing_in_is_required(api: httpx.AsyncClient) -> None:
    response = await api.get(OVERVIEW)
    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")


async def test_unknown_parameters_are_422(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = (await add_staff(db_session)).id
    response = await api.get(OVERVIEW, params={"since": "2026-01-01"}, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    unreachable_sessions: AsyncSession,
) -> None:
    member = (await add_staff(db_session)).id

    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(OVERVIEW, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


async def _counts(api: httpx.AsyncClient, staff: uuid.UUID) -> dict[str, Any]:
    response = await api.get(OVERVIEW, headers=signed_in(staff))
    assert response.status_code == 200, response.json()
    return dict(response.json())


async def test_open_offers_are_those_holding_a_seat(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Past its time though still stored open, or its request declined: it holds nothing."""
    member = (await add_staff(db_session)).id
    route = await add_route(db_session, status=RouteStatus.ACTIVE)
    now = datetime.now(UTC)
    async with db_session.begin():
        waiting, declined, lapsed = [
            await RouteRequestFactory().create(
                db_session, kind="route", route_id=route, from_area_name=None,
                to_area_name=None, **values,
            )
            for values in (
                {}, {"status": RequestStatus.DECLINED, "decline_reason": "Moved."}, {},
            )
        ]  # fmt: skip
    async with db_session.begin():
        for request, expires in (
            (waiting, now + timedelta(hours=1)),
            (declined, now + timedelta(hours=1)),
            (lapsed, now - timedelta(hours=1)),  # never stored expired yet
        ):
            db_session.add(SeatOffer(route_id=route, request_id=request.id,
                                     start_date=date(2026, 12, 1), expires_at=expires))  # fmt: skip
    assert (await _counts(api, member))["seat_offers"] == {"open": 1}


async def test_ready_to_launch_counts_who_a_launch_would_start(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Launch progress's rule: reserved from the launch day or earlier, not gone before."""
    member = (await add_staff(db_session)).id
    route = await add_route(db_session, status=RouteStatus.FORMING, launch_minimum=2)
    await _seat(db_session, route)
    await _seat(db_session, route, start_date=date(2027, 1, 1))  # after 1 December
    await _seat(db_session, route, start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 30))  # gone before it  # fmt: skip
    assert (await _counts(api, member))["routes"]["ready_to_launch"] == 0
    await _seat(db_session, route, start_date=date(2026, 11, 1))
    assert (await _counts(api, member))["routes"]["ready_to_launch"] == 1


async def test_on_the_1st_the_launch_day_is_today(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = (await add_staff(db_session)).id
    route = await add_route(db_session, status=RouteStatus.FORMING, launch_minimum=1)
    await _seat(db_session, route, start_date=date(2026, 12, 1))
    monkeypatch.setattr(schedule, "addis_today", lambda: date(2026, 12, 1))
    counts = await _counts(api, member)
    assert (counts["as_of"], counts["routes"]["ready_to_launch"]) == ("2026-12-01", 1)
