"""GZ-021: seats, counted per day.

A rider holds a seat on every day from their start date to their end date. "This month"
is today to the end of the month, "next month" all of it; each shows its busiest day, so
company riders swapping mid-month take one seat, not two. Today is 15 November 2026
(conftest.py).
"""

import uuid
from datetime import date
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.routes.models import AssignmentStatus
from tests.routes.support import add_company, add_corridor, add_route, add_seat, route_path
from tests.staff.support import add_staff, signed_in

ACTIVE, RESERVED, ENDED = AssignmentStatus.ACTIVE, AssignmentStatus.RESERVED, AssignmentStatus.ENDED


async def _seats(
    api: httpx.AsyncClient, staff_id: uuid.UUID, route_id: uuid.UUID
) -> dict[str, object]:
    body = (await api.get(route_path(route_id), headers=signed_in(staff_id))).json()
    return {key: body[key] for key in ("seats_taken", "seats_free", "reserved")}


async def test_seats_are_counted_on_the_busiest_day(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, seats=14)

    async def seat(**values: object) -> None:
        await add_seat(db_session, route, **values)

    await seat(start_date=date(2026, 10, 1), status=ACTIVE)  # rides on, both months
    await seat(start_date=date(2026, 11, 1), end_date=date(2026, 11, 30), status=ACTIVE)  # leaves
    await seat(start_date=date(2026, 12, 1), status=RESERVED)  # joins next month
    await seat(start_date=date(2026, 10, 1), status=ENDED)  # cancelled: holds nothing
    await seat(start_date=date(2026, 11, 1), end_date=date(2026, 11, 10), status=ACTIVE)  # gone
    await seat(start_date=date(2026, 11, 1), end_date=date(2026, 11, 10), status=ACTIVE)  # gone
    # (Before today the busiest day held 4: "this month" counts from today.)
    # A company-style swap: one rider until the 25th, another from the 26th.
    await seat(start_date=date(2026, 11, 20), end_date=date(2026, 11, 25), status=ACTIVE)
    await seat(start_date=date(2026, 11, 26), status=ACTIVE)
    assert await _seats(api, member.id, route) == {
        "seats_taken": {"this_month": 3, "next_month": 3},
        "seats_free": {"this_month": 11, "next_month": 11},
        "reserved": 1,
    }


async def test_a_seat_freed_at_the_end_of_the_month_is_free_next_month(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, seats=2, launch_minimum=1)
    await add_seat(db_session, route, start_date=date(2026, 9, 1), status=ACTIVE)
    await add_seat(
        db_session, route, start_date=date(2026, 9, 1), end_date=date(2026, 11, 30), status=ACTIVE
    )
    seats = await _seats(api, member.id, route)
    assert seats["seats_free"] == {"this_month": 0, "next_month": 1}


async def test_seats_cannot_drop_below_seats_taken_from_today(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, seats=14, launch_minimum=2)
    for _ in range(3):
        await add_seat(db_session, route, start_date=date(2027, 1, 1), status=RESERVED)
    await add_seat(  # taken only before today: no longer counts
        db_session, route, start_date=date(2026, 1, 1), end_date=date(2026, 11, 14), status=ACTIVE
    )
    too_few = await api.patch(route_path(route), json={"seats": 2}, headers=signed_in(member_id))
    assert (too_few.status_code, too_few.json()["code"]) == (409, "SEATS_BELOW_TAKEN")
    enough = await api.patch(route_path(route), json={"seats": 3}, headers=signed_in(member_id))
    assert enough.status_code == 200


async def test_the_reserved_count_is_launch_progress(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, launch_minimum=12)
    for _ in range(9):  # for 1 December, the next launch day
        await add_seat(db_session, route, status=RESERVED)
    for _ in range(4):  # for January: they don't help a December launch
        await add_seat(db_session, route, start_date=date(2027, 1, 1), status=RESERVED)
    body = (await api.get(route_path(route), headers=signed_in(member.id))).json()
    assert (body["reserved"], body["launch_minimum"]) == (9, 12)  # "9 of 12"


async def test_what_riders_depend_on_cannot_change(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    company, other_company = await add_company(db_session), await add_company(db_session)
    other_corridor = await add_corridor(db_session)
    cases: list[tuple[dict[str, Any], dict[str, str]]] = [
        ({}, {"corridor_id": str(other_corridor)}),
        ({"route_type": "student", "school_name": "ICS"}, {"school_name": "Sandford"}),
        (
            {"route_type": "corporate", "company_id": company, "launch_minimum": None},
            {"company_id": str(other_company)},
        ),
    ]
    for values, change in cases:
        route = await add_route(db_session, **values)
        await add_seat(db_session, route, start_date=date(2026, 11, 1), status=ACTIVE)
        response = await api.patch(route_path(route), json=change, headers=signed_in(member_id))
        assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_RIDERS"), change


async def test_riders_who_have_left_dont_hold_the_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    await add_seat(db_session, route, start_date=date(2026, 1, 1), end_date=date(2026, 10, 31))
    await add_seat(db_session, route, status=ENDED)
    other = await add_corridor(db_session)
    response = await api.patch(
        route_path(route), json={"corridor_id": str(other)}, headers=signed_in(member.id)
    )
    assert response.status_code == 200
