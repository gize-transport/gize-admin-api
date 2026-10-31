"""GZ-024: a route's status, moved by staff.

`public` and `student` routes: draft → forming (needs both prices) → active (launched for
the 1st of a month once enough riders are reserved for it) ⇄ paused; forming → draft while
nobody is reserved. `corporate` routes: draft → active (with an active company) ⇄ paused.
Repeating a move changes nothing. Today is 15 November 2026 (conftest.py).
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
from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.routes.support import ROUTES, add_company, add_route, add_seat, route_path
from tests.staff.support import add_staff, audit_entries, signed_in
from tests.support import SettingsFactory

ACTIVE, RESERVED, ENDED = AssignmentStatus.ACTIVE, AssignmentStatus.RESERVED, AssignmentStatus.ENDED
PRICED = {"price_round_trip": 2400, "price_one_way": 1400}
DECEMBER, JANUARY = date(2026, 12, 1), date(2027, 1, 1)
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


def status_path(route_id: uuid.UUID) -> str:
    return f"{route_path(route_id)}/status"


def _launch(on: date = DECEMBER) -> dict[str, str]:
    return {"status": "active", "launch_date": on.isoformat()}


async def _corporate(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    company = await add_company(session, is_active=values.pop("company_active", True))
    return await add_route(
        session, route_type="corporate", company_id=company, launch_minimum=None, **values
    )


async def _seats(session: AsyncSession, route_id: uuid.UUID) -> list[tuple[date, Any, Any]]:
    """(start, end, status) of each seat, by start date, end date, then status."""
    async with session.begin():
        rows = await session.execute(
            select(RouteAssignment.start_date, RouteAssignment.end_date, RouteAssignment.status)
            .where(RouteAssignment.route_id == route_id)
            .order_by(
                RouteAssignment.start_date,
                RouteAssignment.end_date.nulls_last(),
                RouteAssignment.status,
            )
        )
        return [(start, end, status) for start, end, status in rows]


async def test_a_priced_route_starts_forming(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    for route_type, extra in (("public", {}), ("student", {"school_name": "ICS"})):
        route = await add_route(db_session, route_type=route_type, **PRICED, **extra)
        response = await api.put(
            status_path(route), json={"status": "forming"}, headers=signed_in(member.id)
        )
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["status"] == "forming"
        (entry,) = await audit_entries(db_session, route)
        assert (entry.action, entry.actor_staff_id) == ("route.status_changed", member.id)
        assert (entry.before, entry.after) == ({"status": "draft"}, {"status": "forming"})


@pytest.mark.parametrize(
    "prices", [{}, {"price_round_trip": 2400}, {"price_one_way": 1400}], ids=str
)
async def test_a_route_without_both_prices_is_not_ready(
    api: httpx.AsyncClient, db_session: AsyncSession, prices: dict[str, int]
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, **prices)
    response = await api.put(
        status_path(route), json={"status": "forming"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_NOT_READY")
    assert await audit_entries(db_session, route) == []


async def test_a_forming_route_goes_back_to_draft_while_nobody_is_reserved(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    empty = await add_route(db_session, status="forming", **PRICED)
    left = await add_route(db_session, status="forming", **PRICED)
    await add_seat(db_session, left, status=ENDED)  # someone who reserved, then cancelled
    gone = await add_route(db_session, status="forming", **PRICED)
    # Still marked reserved, but for October only: the same rule as a delete's lets it go.
    await add_seat(db_session, gone, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))
    held = await add_route(db_session, status="forming", **PRICED)
    await add_seat(db_session, held, start_date=date(2027, 3, 1))  # even far ahead
    draft = {"status": "draft"}
    for route in (empty, left, gone):
        response = await api.put(status_path(route), json=draft, headers=signed_in(member_id))
        assert response.json()["status"] == "draft"
    refused = await api.put(status_path(held), json=draft, headers=signed_in(member_id))
    assert (refused.status_code, refused.json()["code"]) == (409, "ROUTE_HAS_RIDERS")


async def test_a_launch_starts_the_reserved_riders(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Riders reserved for the launch month or earlier count and start on the launch day;
    later ones start on their own date; a reservation that ended before the launch lapses."""
    member = await add_staff(db_session)
    route = await add_route(db_session, status="forming", launch_minimum=2, **PRICED)
    november = date(2026, 11, 1)
    await add_seat(db_session, route, start_date=november)  # waited since November
    await add_seat(db_session, route, start_date=DECEMBER)
    await add_seat(db_session, route, start_date=JANUARY)
    await add_seat(db_session, route, start_date=DECEMBER, status=ENDED)
    await add_seat(db_session, route, start_date=november, end_date=date(2026, 11, 30))
    response = await api.put(status_path(route), json=_launch(), headers=signed_in(member.id))
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["reserved"]) == ("active", 0)
    assert body["seats_taken"] == {"this_month": 0, "next_month": 2}
    assert await _seats(db_session, route) == [
        (november, date(2026, 11, 30), ENDED),
        (DECEMBER, None, ACTIVE),
        (DECEMBER, None, ACTIVE),
        (DECEMBER, None, ENDED),
        (JANUARY, None, ACTIVE),
    ]
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.actor_staff_id) == ("route.launched", member.id)
    assert (entry.before, entry.after) == (
        {"status": "forming", "launch_date": None, "riders_started": None, "lapsed": None},
        {"status": "active", "launch_date": "2026-12-01", "riders_started": 3, "lapsed": 1},
    )


async def test_a_launch_needs_the_minimum_reserved_for_that_month(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=3, **PRICED)
    for start in (DECEMBER, DECEMBER, JANUARY):
        await add_seat(db_session, route, start_date=start)
    # Ended before December: it doesn't count.
    await add_seat(db_session, route, start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
    before = await _seats(db_session, route)
    too_few = await api.put(status_path(route), json=_launch(), headers=signed_in(member_id))
    assert (too_few.status_code, too_few.json()["code"]) == (409, "NOT_ENOUGH_RIDERS")
    assert await _seats(db_session, route) == before
    assert await audit_entries(db_session, route) == []
    # January's reservation counts for a January launch.
    later = await api.put(status_path(route), json=_launch(JANUARY), headers=signed_in(member_id))
    assert later.json()["status"] == "active"
    assert {start for start, _, status in await _seats(db_session, route) if status is ACTIVE} == {
        JANUARY
    }


async def test_a_route_launches_today_on_the_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "addis_today", lambda: DECEMBER)
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=DECEMBER)
    response = await api.put(status_path(route), json=_launch(), headers=signed_in(member_id))
    assert response.json()["status"] == "active"
    assert response.json()["seats_taken"]["this_month"] == 1


@pytest.mark.parametrize(
    ("launch_date", "why"),
    [
        ("2026-12-02", "not the 1st"),
        ("2026-11-01", "before today"),
        ("2027-12-01", "more than a year ahead"),
        ("9999-12-01", "a mistyped year"),
        ("2026-12", "not a date"),
        (None, "null"),
        ("left out", "missing"),
    ],
)
async def test_a_launch_date_is_a_coming_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, launch_date: str | None, why: str
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=date(2026, 11, 1))
    body: dict[str, object] = {"status": "active"}
    if launch_date != "left out":
        body["launch_date"] = launch_date
    response = await api.put(status_path(route), json=body, headers=signed_in(member_id))
    assert response.status_code == 422, why
    assert [e["field"] for e in response.json()["errors"]] == ["/launch_date"], why


async def test_a_launch_may_be_a_year_ahead(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", launch_minimum=1, **PRICED)
    await add_seat(db_session, route, start_date=DECEMBER)
    response = await api.put(
        status_path(route), json=_launch(date(2027, 11, 1)), headers=signed_in(member_id)
    )
    assert response.json()["status"] == "active"
    assert [start for start, _, _ in await _seats(db_session, route)] == [date(2027, 11, 1)]


async def test_a_bad_status_is_one_error(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="forming", **PRICED)
    body = {"status": "closed", "launch_date": "2026-12-01"}
    response = await api.put(status_path(route), json=body, headers=signed_in(member_id))
    assert [e["field"] for e in response.json()["errors"]] == ["/status"]


async def test_a_launch_date_is_only_for_a_launch(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    paused = await add_route(db_session, status="paused", **PRICED)
    corporate = await _corporate(db_session)
    for route in (paused, corporate):
        response = await api.put(status_path(route), json=_launch(), headers=signed_in(member_id))
        assert [e["field"] for e in response.json()["errors"]] == ["/launch_date"]
    forming = await add_route(db_session, **PRICED)
    body = {"status": "forming", "launch_date": "2026-12-01"}  # refused before any lookup
    response = await api.put(status_path(forming), json=body, headers=signed_in(member_id))
    assert [e["field"] for e in response.json()["errors"]] == ["/launch_date"]


async def test_running_routes_pause_and_resume(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    public = await add_route(db_session, status="active", **PRICED)
    corporate = await _corporate(db_session, status="active")
    for route in (public, corporate):
        for status in ("paused", "active"):
            response = await api.put(
                status_path(route), json={"status": status}, headers=signed_in(member_id)
            )
            assert response.json()["status"] == status
        changes = [(e.before, e.after) for e in await audit_entries(db_session, route)]
        assert sorted(changes, key=str) == sorted(
            [
                ({"status": "active"}, {"status": "paused"}),
                ({"status": "paused"}, {"status": "active"}),
            ],
            key=str,
        )


async def test_a_corporate_route_starts_with_an_active_company(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    ready = await _corporate(db_session)
    inactive = await _corporate(db_session, company_active=False)
    active = {"status": "active"}
    response = await api.put(status_path(ready), json=active, headers=signed_in(member_id))
    assert response.json()["status"] == "active"
    refused = await api.put(status_path(inactive), json=active, headers=signed_in(member_id))
    assert (refused.status_code, refused.json()["code"]) == (409, "ROUTE_NOT_READY")


@pytest.mark.parametrize(
    ("route_type", "now", "to"),
    [
        ("public", "draft", "active"),
        ("public", "draft", "paused"),
        ("public", "forming", "paused"),
        ("public", "active", "draft"),
        ("public", "active", "forming"),
        ("public", "paused", "draft"),
        ("public", "paused", "forming"),
        ("corporate", "draft", "forming"),
        ("corporate", "draft", "paused"),
        ("corporate", "active", "draft"),
        ("corporate", "paused", "draft"),
    ],
)
async def test_other_moves_are_refused(
    api: httpx.AsyncClient, db_session: AsyncSession, route_type: str, now: str, to: str
) -> None:
    member_id = (await add_staff(db_session)).id
    if route_type == "corporate":
        route = await _corporate(db_session, status=now)
    else:
        route = await add_route(db_session, status=now, **PRICED)
    response = await api.put(status_path(route), json={"status": to}, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (409, "INVALID_STATUS_CHANGE")
    assert await audit_entries(db_session, route) == []


@pytest.mark.parametrize(
    "body",
    [
        {"status": "draft"},
        {"status": "forming"},
        {"status": "active"},
        _launch(),  # a retried launch, even with another date
        _launch(JANUARY),
        {"status": "paused"},
    ],
    ids=str,
)
async def test_repeating_a_move_changes_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, str]
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status=body["status"])  # no prices: no check runs
    before = (await api.get(route_path(route), headers=signed_in(member_id))).json()
    response = await api.put(status_path(route), json=body, headers=signed_in(member_id))
    assert response.status_code == 200
    assert response.json() == before
    assert await audit_entries(db_session, route) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": "closed"},
        {"status": None},
        {"status": "forming", "note": "x"},
        {"status": "active", "launch_date": 20261201},
        {"status": "active", "launch_date": "2026-12-01T00:00:00"},
        [],
    ],
    ids=str,
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, **PRICED)
    response = await api.put(status_path(route), json=body, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert await audit_entries(db_session, route) == []


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    target = await add_route(db_session, deleted_at=START, **PRICED) if deleted else uuid.uuid4()
    response = await api.put(
        status_path(target), json={"status": "forming"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_staff(db_session, is_active=False)).id
    route = await add_route(db_session, **PRICED)
    body = {"status": "forming"}
    assert (await api.put(status_path(route), json=body)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        response = await api.put(status_path(route), json=body, headers=signed_in(caller))
        assert response.status_code == 403
    assert await audit_entries(db_session, route) == []


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.put(
        status_path(uuid.UUID(int=1)), json={"status": "forming"}, headers=signed_in(uuid.uuid4())
    )
    assert response.status_code == 503


def test_status_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}/status"]["put"]
    assert operation["operationId"] == "routes_set_status"
    assert operation["responses"]["409"]["x-error-codes"] == [
        "INVALID_STATUS_CHANGE",
        "NOT_ENOUGH_RIDERS",
        "ROUTE_HAS_RIDERS",
        "ROUTE_NOT_READY",
    ]
    body = schema["components"]["schemas"]["RouteStatusSet"]
    assert body["required"] == ["status"]
    assert body["additionalProperties"] is False
