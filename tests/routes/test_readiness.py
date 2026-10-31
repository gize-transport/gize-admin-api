"""GZ-067: what a route still needs before each status change, and which months it can
launch, so the admin frontend never works the rules out itself.

Every route lists `status_changes`: the moves its type allows from its status, each with
`ready` and what blocks it (`blocked_by`), by the same rules `PUT /status` applies. A launch
is judged for the earliest launch day (`launch_date`); `GET /launch-months` judges every
month a launch may name. Both are what the API would answer now: a change can still be
refused if something moves meanwhile. Today is 15 November 2026 (conftest.py).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import schedule
from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment
from tests.routes.support import ROUTES, add_company, add_route, add_seat, route_path
from tests.staff.support import add_admin, add_staff, signed_in
from tests.support import SettingsFactory

ENDED = AssignmentStatus.ENDED
PRICED = {"price_round_trip": 2400, "price_one_way": 1400}
DECEMBER, JANUARY, FEBRUARY = date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1)
DELETED = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


def _change(to: str, *blocked_by: str, launch_date: date | None = None) -> dict[str, object]:
    return {
        "to": to,
        "ready": not blocked_by,
        "blocked_by": list(blocked_by),
        "launch_date": None if launch_date is None else launch_date.isoformat(),
    }


def months_path(route_id: uuid.UUID) -> str:
    return f"{route_path(route_id)}/launch-months"


async def _corporate(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    company = await add_company(session, is_active=values.pop("company_active", True))
    return await add_route(
        session, route_type="corporate", company_id=company, launch_minimum=None, **values
    )


async def _changes(api: httpx.AsyncClient, member_id: uuid.UUID, route: uuid.UUID) -> Any:  # noqa: ANN401
    response = await api.get(route_path(route), headers=signed_in(member_id))
    assert response.status_code == 200
    return response.json()["status_changes"]


# Status changes


async def test_a_draft_route_needs_both_prices_to_start_forming(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    for prices in ({}, {"price_round_trip": 2400}, {"price_one_way": 1400}):
        route = await add_route(db_session, **prices)
        assert await _changes(api, member_id, route) == [_change("forming", "PRICES_MISSING")], (
            prices
        )
    priced = await add_route(db_session, route_type="student", school_name="ICS", **PRICED)
    assert await _changes(api, member_id, priced) == [_change("forming")]


async def test_a_forming_route_shows_launch_progress_and_who_holds_it(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Back to draft is blocked by anyone reserved, even far ahead; a launch on the earliest
    launch day (1 December) by too few riders reserved for it."""
    member_id = (await add_staff(db_session)).id
    empty = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    assert await _changes(api, member_id, empty) == [
        _change("draft"),
        _change("active", "NOT_ENOUGH_RIDERS", launch_date=DECEMBER),
    ]
    later = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, later, start_date=JANUARY)
    assert await _changes(api, member_id, later) == [
        _change("draft", "ROUTE_HAS_RIDERS"),
        _change("active", "NOT_ENOUGH_RIDERS", launch_date=DECEMBER),
    ]
    full = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    for _ in range(2):
        await add_seat(db_session, full, start_date=DECEMBER)
    assert await _changes(api, member_id, full) == [
        _change("draft", "ROUTE_HAS_RIDERS"),
        _change("active", launch_date=DECEMBER),
    ]


async def test_riders_who_left_dont_hold_a_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    left = await add_route(db_session, status="forming", **PRICED)
    await add_seat(db_session, left, status=ENDED)
    await add_seat(db_session, left, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))
    assert (await _changes(api, member_id, left))[0] == _change("draft")


async def test_the_earliest_launch_day_is_today_on_the_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "addis_today", lambda: DECEMBER)
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=DECEMBER)
    assert (await _changes(api, member_id, route))[1] == _change("active", launch_date=DECEMBER)


async def test_running_routes_pause_and_resume(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    active = await add_route(db_session, status="active", **PRICED)
    paused = await add_route(db_session, status="paused", **PRICED)
    assert await _changes(api, member_id, active) == [_change("paused")]
    assert await _changes(api, member_id, paused) == [_change("active")]


async def test_a_corporate_route_runs_only_for_an_active_company(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    cases = [
        ("draft", True, [_change("active")]),
        ("draft", False, [_change("active", "COMPANY_INACTIVE")]),
        ("active", False, [_change("paused")]),
        ("paused", True, [_change("active")]),
        ("paused", False, [_change("active", "COMPANY_INACTIVE")]),
    ]
    for status, company_active, expected in cases:
        route = await _corporate(db_session, status=status, company_active=company_active)
        assert await _changes(api, member_id, route) == expected, (status, company_active)


async def test_every_answer_matches_what_the_change_does(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """For each listed change, `ready` is exactly whether `PUT /status` accepts it, and a
    refusal's code follows from what blocks it."""
    member_id = (await add_staff(db_session)).id
    refusal = {
        "PRICES_MISSING": "ROUTE_NOT_READY",
        "COMPANY_INACTIVE": "ROUTE_NOT_READY",
        "ROUTE_HAS_RIDERS": "ROUTE_HAS_RIDERS",
        "NOT_ENOUGH_RIDERS": "NOT_ENOUGH_RIDERS",
    }
    held = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, held, start_date=DECEMBER)
    short = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    await add_seat(db_session, short, start_date=DECEMBER)
    routes = [
        await add_route(db_session),
        await add_route(db_session, **PRICED),
        held,
        short,
        await add_route(db_session, status="forming", **PRICED),
        await add_route(db_session, status="active", **PRICED),
        await add_route(db_session, status="paused", **PRICED),
        await _corporate(db_session),
        await _corporate(db_session, company_active=False),
        await _corporate(db_session, status="paused", company_active=False),
    ]
    tried = 0
    for route in routes:
        for change in await _changes(api, member_id, route):
            body = {"status": change["to"]}
            if change["launch_date"] is not None:
                body["launch_date"] = change["launch_date"]
            # Each change on a route of its own, so one move doesn't change the next.
            twin = await _twin(db_session, route)
            response = await api.put(
                f"{route_path(twin)}/status", json=body, headers=signed_in(member_id)
            )
            if change["ready"]:
                assert response.status_code == 200, (change, response.json())
            else:
                assert response.status_code == 409, change
                assert response.json()["code"] == refusal[change["blocked_by"][0]], change
            tried += 1
    assert tried == 13


async def _twin(session: AsyncSession, route_id: uuid.UUID) -> uuid.UUID:
    """A copy of the route (status, prices, company, minimum and seats held) to change."""
    async with session.begin():
        route = await session.get_one(Route, route_id)
        values = {
            "status": route.status,
            "route_type": route.route_type,
            "school_name": route.school_name,
            "company_id": route.company_id,
            "launch_minimum": route.launch_minimum,
            "price_round_trip": route.price_round_trip,
            "price_one_way": route.price_one_way,
        }
        seats = (
            await session.execute(
                select(
                    RouteAssignment.start_date, RouteAssignment.end_date, RouteAssignment.status
                ).where(RouteAssignment.route_id == route_id)
            )
        ).all()
    twin = await add_route(session, **values)
    for start, end, status in seats:
        await add_seat(session, twin, start_date=start, end_date=end, status=status)
    return twin


async def test_lists_and_changes_show_the_same_answers(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """In the list, and in what a change returns: the route as it is after the change."""
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=JANUARY)
    listed = (await api.get(ROUTES, headers=signed_in(member_id))).json()["items"]
    assert [item["status_changes"] for item in listed] == [[_change("forming")]]
    moved = await api.put(
        f"{route_path(route)}/status", json={"status": "forming"}, headers=signed_in(member_id)
    )
    assert moved.json()["status_changes"] == [
        _change("draft", "ROUTE_HAS_RIDERS"),
        _change("active", "NOT_ENOUGH_RIDERS", launch_date=DECEMBER),
    ]


# Launch months


def _month(launch_date: date, reserved: int, *, ready: bool) -> dict[str, object]:
    return {"launch_date": launch_date.isoformat(), "reserved": reserved, "ready": ready}


async def test_staff_see_which_months_a_forming_route_can_launch(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Every 1st from the earliest launch day to a year ahead (the dates a launch may
    name), with the riders a launch that day would start."""
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    await add_seat(db_session, route, start_date=date(2026, 11, 1))  # waited since November
    await add_seat(db_session, route, start_date=JANUARY)
    await add_seat(db_session, route, start_date=DECEMBER, end_date=date(2027, 1, 31))
    await add_seat(db_session, route, start_date=DECEMBER, status=ENDED)  # cancelled
    await add_seat(db_session, route, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
    response = await api.get(months_path(route), headers=signed_in(member_id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert (body["status"], body["launch_minimum"]) == ("forming", 2)
    later = [date(2027, month, 1) for month in range(3, 12)]
    assert body["months"] == [
        _month(DECEMBER, 2, ready=True),
        _month(JANUARY, 3, ready=True),
        _month(FEBRUARY, 2, ready=True),
        *(_month(day, 2, ready=True) for day in later),
    ]
    short = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    await add_seat(db_session, short, start_date=DECEMBER)
    await add_seat(db_session, short, start_date=FEBRUARY)
    months = (await api.get(months_path(short), headers=signed_in(member_id))).json()["months"]
    assert [(m["launch_date"], m["ready"]) for m in months[:3]] == [
        ("2026-12-01", False),
        ("2027-01-01", False),
        ("2027-02-01", True),
    ]


async def test_on_the_1st_the_months_start_today(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "addis_today", lambda: DECEMBER)
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    months = (await api.get(months_path(route), headers=signed_in(member_id))).json()["months"]
    assert [m["launch_date"] for m in (months[0], months[-1])] == ["2026-12-01", "2027-12-01"]
    assert len(months) == 13


async def test_only_a_forming_route_is_ready_to_launch(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Other routes still show their reservations; none is ready (corporate routes start
    without a launch: `PUT /status` with `active`)."""
    member_id = (await add_staff(db_session)).id
    draft = await add_route(db_session, launch_minimum=1, **PRICED)
    await add_seat(db_session, draft, start_date=DECEMBER)
    corporate = await _corporate(db_session)
    for route, minimum, reserved in ((draft, 1, 1), (corporate, None, 0)):
        body = (await api.get(months_path(route), headers=signed_in(member_id))).json()
        assert body["launch_minimum"] == minimum
        assert body["months"][0] == _month(DECEMBER, reserved, ready=False)
        assert not any(month["ready"] for month in body["months"])


async def test_each_month_matches_what_a_launch_does(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=JANUARY)
    months = (await api.get(months_path(route), headers=signed_in(member_id))).json()["months"]
    path = f"{route_path(route)}/status"
    for month in months[:2]:
        response = await api.put(
            path,
            json={"status": "active", "launch_date": month["launch_date"]},
            headers=signed_in(member_id),
        )
        assert (response.status_code == 200) is month["ready"], month


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_have_no_months(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, deleted_at=DELETED) if deleted else uuid.UUID(int=1)
    response = await api.get(months_path(route), headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_a_malformed_route_id_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    response = await api.get(f"{ROUTES}/not-a-uuid/launch-months", headers=signed_in(member_id))
    assert response.status_code == 422


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    path = months_path(uuid.UUID(int=1))
    assert (await api.get(path)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        assert (await api.get(path, headers=signed_in(caller))).status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(months_path(uuid.UUID(int=1)), headers=signed_in(uuid.uuid4()))
    assert response.status_code == 503


def test_readiness_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}/launch-months"]["get"]
    assert operation["operationId"] == "routes_launch_months"
    schemas = schema["components"]["schemas"]
    assert schemas["RouteBlocker"]["enum"] == [
        "PRICES_MISSING",
        "COMPANY_INACTIVE",
        "ROUTE_HAS_RIDERS",
        "NOT_ENOUGH_RIDERS",
    ]
    assert "status_changes" in schemas["RouteRead"]["required"]
    assert set(schemas["RouteLaunchMonths"]["properties"]) == {
        "status",
        "launch_minimum",
        "months",
    }
