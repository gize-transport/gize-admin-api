"""GZ-027: where the demand is, to decide where the next route or van goes.

- Area pairs: open `new_areas` requests not matched to a route yet, per pair (a name not
  listed yet counts ignoring case).
- Routes: open requests per route, each once: the route it was matched to, else the one
  asked for; next to the route's free seats next month.

Busiest first, at most 100 of each. Today is 15 November 2026 (conftest.py).
"""

from datetime import UTC, date, datetime

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.routes.models import AssignmentStatus
from tests.factories import AreaFactory, RouteRequestFactory
from tests.route_requests.support import REQUESTS, add_request
from tests.routes.support import add_route, add_seat
from tests.staff.support import add_staff, signed_in

SUMMARY = f"{REQUESTS}/summary"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def test_demand_per_area_pair(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    async with db_session.begin():
        ayat = await AreaFactory().create(db_session, name="Ayat")
        ayat_id = ayat.id
    route = await add_route(db_session)
    for day, name in enumerate(("Bole", "bole", "BOLE"), start=1):
        await add_request(
            db_session, from_area_id=ayat_id, from_area_name=None, to_area_name=name,
            created_at=datetime(2026, 11, day, tzinfo=UTC),
        )  # fmt: skip
    await add_request(db_session, from_area_name="CMC", to_area_name="Piassa")
    # Not counted: matched, closed, deleted, or the other way round (a pair of its own).
    await add_request(
        db_session, from_area_id=ayat_id, from_area_name=None, to_area_name="Bole",
        status="matched", matched_route_id=route,
    )  # fmt: skip
    await add_request(
        db_session, from_area_id=ayat_id, from_area_name=None, to_area_name="Bole",
        status="declined", decline_reason="Too far.",
    )  # fmt: skip
    await add_request(
        db_session, from_area_id=ayat_id, from_area_name=None, to_area_name="Bole",
        deleted_at=START,
    )  # fmt: skip
    response = await api.get(SUMMARY, headers=signed_in(member_id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["area_pairs"] == [
        {
            "from_area": {"id": str(ayat_id), "name": "Ayat"},
            "from_area_name": None,
            "to_area": None,
            "to_area_name": "Bole",  # the first spelling recorded
            "open": 3,
        },
        {
            "from_area": None,
            "from_area_name": "CMC",
            "to_area": None,
            "to_area_name": "Piassa",
            "open": 1,
        },
    ]
    assert body["area_pairs_truncated"] is False


async def test_demand_per_route_next_to_its_free_seats(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    full = await add_route(db_session, code="AYAT-01", status="active", seats=2, launch_minimum=2)
    for _ in range(2):
        await add_seat(
            db_session, full, start_date=date(2026, 9, 1), status=AssignmentStatus.ACTIVE
        )
    van = await add_route(db_session, code="AYAT-02", status="forming", seats=14)
    asked = {"kind": "route", "route_id": full, "from_area_name": None, "to_area_name": None}
    await add_request(db_session, **asked)
    await add_request(db_session, **asked)
    await add_request(db_session, **asked, status="matched", matched_route_id=van)  # counts once
    await add_request(db_session, status="matched", matched_route_id=van)
    await add_request(db_session, status="matched", matched_route_id=van)
    await add_request(db_session, **asked, status="fulfilled")  # closed
    body = (await api.get(SUMMARY, headers=signed_in(member_id))).json()
    assert body["routes"] == [
        {
            "route": {"id": str(van), "code": "AYAT-02"},
            "status": "forming",
            "open": 3,
            "seats_free_next_month": 14,
        },
        {
            "route": {"id": str(full), "code": "AYAT-01"},
            "status": "active",
            "open": 2,
            "seats_free_next_month": 0,
        },
    ]
    assert body["routes_truncated"] is False


async def test_the_summary_is_bounded(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    async with db_session.begin():
        await RouteRequestFactory().create_batch(db_session, 101)  # 101 pairs of one
    body = (await api.get(SUMMARY, headers=signed_in(member_id))).json()
    assert (len(body["area_pairs"]), body["area_pairs_truncated"]) == (100, True)


async def test_the_summary_takes_the_same_queries_however_many_routes(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for routes in (1, 10):
        for _ in range(routes):
            route = await add_route(db_session, status="active")
            await add_request(
                db_session, kind="route", route_id=route, from_area_name=None, to_area_name=None
            )
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(SUMMARY, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts[0] == counts[1]


async def test_an_empty_summary(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    body = (await api.get(SUMMARY, headers=signed_in(member_id))).json()
    assert body == {
        "area_pairs": [],
        "area_pairs_truncated": False,
        "routes": [],
        "routes_truncated": False,
    }
