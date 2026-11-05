"""GZ-035: staff give riders a seat, move them, and end it ("today" is 15 November 2026).

`PUT /v1/riders/{id}/seat` places a rider on a route (or moves them): an adult on a public
route that takes their trip, from a 1st (the next by default, at most a year ahead): a
reservation on a `forming` route, a place on an `active` one. Seats are counted on every
day from the start, under the route's lock (`ROUTE_FULL`). Placing an adult on an active
route ahead of other people's open requests for it needs a `skip_reason` (audited); their
own open requests for the route are fulfilled. A move ends the old seat the day before
(a reservation just ends: it never rode).

`POST /v1/riders/{id}/seat/end` ends what they hold: adults at a month's end (this month's
by default), reservations at once.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.route_requests.models import RequestKind, RequestStatus, RouteRequest
from gize_api.routes.models import AssignmentStatus, RouteStatus, RouteType
from tests.factories import CompanyFactory, RouteRequestFactory
from tests.registrations.support import Trip, add_route, add_trip, take_seats
from tests.riders.support import (
    add_rider,
    add_seat,
    end_path,
    path,
    seat_path,
    seats_of,
)
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def _place(
    api: httpx.AsyncClient, staff: uuid.UUID, rider: uuid.UUID, route: uuid.UUID, **more: str
) -> httpx.Response:
    return await api.put(
        seat_path(rider), json={"route_id": str(route), **more}, headers=with_key(staff)
    )


async def _end(
    api: httpx.AsyncClient, staff: uuid.UUID, rider: uuid.UUID, **body: str
) -> httpx.Response:
    return await api.post(end_path(rider), json=body, headers=with_key(staff))


async def _request(session: AsyncSession, route: uuid.UUID, **values: object) -> uuid.UUID:
    """An open request for the route by someone else."""
    async with session.begin():
        request = await RouteRequestFactory().create(
            session,
            kind=RequestKind.ROUTE,
            route_id=route,
            from_area_name=None,
            to_area_name=None,
            **values,
        )
        return request.id


# Placing an adult


async def test_an_adult_on_a_forming_route_gets_a_reservation_from_the_next_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, code="BOLE-MEX-01")
    rider = await add_rider(db_session, trip)
    response = await _place(api, member, rider, route)
    assert response.status_code == 200, response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["seat"] == {
        "route_id": str(route),
        "route_code": "BOLE-MEX-01",
        "status": "reserved",
        "start_date": "2026-12-01",
        "end_date": None,
    }
    assert response.json()["next_seat"] is None
    (seat,) = await seats_of(db_session, rider)
    (entry,) = await audit_entries(db_session, seat.id)
    assert (entry.action, entry.actor_staff_id, entry.before) == (
        "route_assignment.created",
        member,
        None,
    )
    assert entry.after == {
        "route_id": str(route),
        "rider_id": str(rider),
        "start_date": "2026-12-01",
        "end_date": None,
        "status": "reserved",
        "skip_reason": None,
    }


async def test_an_adult_on_an_active_route_gets_a_place_from_a_chosen_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    response = await _place(api, member, rider, route, start_date="2027-01-01")
    assert response.status_code == 200, response.json()
    seat = response.json()["seat"]
    assert (seat["status"], seat["start_date"]) == ("active", "2027-01-01")


@pytest.mark.parametrize(
    "start",
    ["2026-12-15", "2026-11-01", "2027-12-01", "15/12/2026"],
    ids=["not a 1st", "past", "over a year ahead", "not a date"],
)
async def test_an_adult_starts_on_a_coming_1st(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, start: str
) -> None:
    route = await add_route(db_session, trip)
    rider = await add_rider(db_session, trip)
    response = await _place(api, member, rider, route, start_date=start)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/start_date"]
    assert await seats_of(db_session, rider) == []


@pytest.mark.parametrize(
    "problem",
    ["unknown", "deleted", "draft", "paused", "student", "corporate", "elsewhere", "saturday"],
)
async def test_a_route_that_does_not_take_the_adult_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, problem: str
) -> None:
    rider = await add_rider(db_session, trip, saturday_addon=problem == "saturday")
    if problem == "unknown":
        route = uuid.uuid4()
    elif problem == "deleted":
        route = await add_route(db_session, trip, deleted_at=datetime.now(UTC))
    elif problem in ("draft", "paused"):
        route = await add_route(db_session, trip, status=RouteStatus(problem))
    elif problem == "student":
        route = await add_route(db_session, trip, route_type=RouteType.STUDENT, school_name="ICS")
    elif problem == "corporate":
        async with db_session.begin():
            company = await CompanyFactory().create(db_session)
        route = await add_route(
            db_session, trip, route_type=RouteType.CORPORATE, company_id=company.id,
            status=RouteStatus.ACTIVE, launch_minimum=None, price_round_trip=None,
            price_one_way=None,
        )  # fmt: skip
    elif problem == "elsewhere":
        rider = await add_rider(db_session, trip, destination_area_id=trip.summit)
        route = await add_route(db_session, trip)
    else:  # the rider has the Saturday add-on; the route offers none
        route = await add_route(db_session, trip)
    response = await _place(api, member, rider, route)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/route_id"]
    assert await seats_of(db_session, rider) == []


async def test_a_full_route_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, seats=2, launch_minimum=2)
    await take_seats(db_session, route, 2)
    rider = await add_rider(db_session, trip)
    response = await _place(api, member, rider, route)
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_FULL")
    assert await seats_of(db_session, rider) == []


async def test_seats_are_counted_on_every_day_from_the_start(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Someone leaving at the end of December frees January, not December."""
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE, seats=1,
                            launch_minimum=1)  # fmt: skip
    leaver = await add_rider(db_session, trip)
    await add_seat(db_session, route, leaver, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1), end_date=date(2026, 12, 31))  # fmt: skip
    rider = await add_rider(db_session, trip)
    december = await _place(api, member, rider, route)
    assert (december.status_code, december.json()["code"]) == (409, "ROUTE_FULL")
    january = await _place(api, member, rider, route, start_date="2027-01-01")
    assert january.status_code == 200


async def test_placing_on_the_route_they_hold_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, route, rider)
    response = await _place(api, member, rider, route)
    assert (response.status_code, response.json()["code"]) == (409, "ALREADY_ON_ROUTE")


# The queue: other people's requests come first


async def test_jumping_open_requests_needs_a_reason(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    await _request(db_session, route)
    await _request(db_session, route, status=RequestStatus.DECLINED, decline_reason="Moved.")
    rider = await add_rider(db_session, trip)
    refused = await _place(api, member, rider, route)
    assert (refused.status_code, refused.json()["code"]) == (409, "ROUTE_HAS_WAITING_REQUESTS")
    placed = await _place(api, member, rider, route, skip_reason="Their employer pays.")
    assert placed.status_code == 200, placed.json()
    (seat,) = await seats_of(db_session, rider)
    assert seat.skip_reason == "Their employer pays."
    (entry,) = await audit_entries(db_session, seat.id)
    assert entry.after is not None
    assert entry.after["skip_reason"] == REDACTED  # free text: may name someone


async def test_a_match_to_the_route_waits_in_the_queue_too(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    async with db_session.begin():
        await RouteRequestFactory().create(
            db_session, status=RequestStatus.MATCHED, matched_route_id=route
        )
    rider = await add_rider(db_session, trip)
    response = await _place(api, member, rider, route)
    assert response.json()["code"] == "ROUTE_HAS_WAITING_REQUESTS"


async def test_the_oldest_requester_needs_no_reason_and_is_fulfilled(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    own = await _request(db_session, route, rider_id=rider,
                         created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    later = await _request(db_session, route, created_at=datetime(2026, 10, 2, tzinfo=UTC))
    response = await _place(api, member, rider, route)
    assert response.status_code == 200, response.json()
    async with db_session.begin():
        statuses = {
            row.id: row.status for row in (await db_session.scalars(select(RouteRequest))).all()
        }
    assert statuses == {own: RequestStatus.FULFILLED, later: RequestStatus.NEW}
    assert [entry.action for entry in await audit_entries(db_session, own)] == [
        "route_request.status_changed"
    ]


async def test_someone_who_asked_earlier_still_comes_first(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await _request(db_session, route, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    await _request(db_session, route, rider_id=rider,
                   created_at=datetime(2026, 10, 2, tzinfo=UTC))  # fmt: skip
    response = await _place(api, member, rider, route)
    assert response.json()["code"] == "ROUTE_HAS_WAITING_REQUESTS"


async def test_a_forming_route_has_no_queue(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip)
    async with db_session.begin():
        await RouteRequestFactory().create(
            db_session, status=RequestStatus.MATCHED, matched_route_id=route
        )
    rider = await add_rider(db_session, trip)
    assert (await _place(api, member, rider, route)).status_code == 200


# Moving


async def test_a_move_ends_the_old_seat_the_day_before(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    old = await add_route(db_session, trip, status=RouteStatus.ACTIVE, code="OLD-01")
    new = await add_route(db_session, trip, status=RouteStatus.ACTIVE, code="NEW-01")
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, old, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip
    response = await _place(api, member, rider, new)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["seat"]["route_code"], body["seat"]["end_date"]) == ("OLD-01", "2026-11-30")
    assert (body["next_seat"]["route_code"], body["next_seat"]["start_date"]) == (
        "NEW-01",
        "2026-12-01",
    )
    first, second = await seats_of(db_session, rider)
    assert (first.status, first.end_date) == (AssignmentStatus.ACTIVE, date(2026, 11, 30))
    assert [entry.action for entry in await audit_entries(db_session, first.id)] == [
        "route_assignment.changed"
    ]
    assert (second.route_id, second.status) == (new, AssignmentStatus.ACTIVE)


async def test_a_moved_reservation_just_ends(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """It never rode: nothing to keep until the month's end."""
    old, new = await add_route(db_session, trip), await add_route(db_session, trip)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, old, rider)
    response = await _place(api, member, rider, new)
    assert response.status_code == 200, response.json()
    assert (response.json()["seat"]["route_id"], response.json()["next_seat"]) == (str(new), None)
    first, _ = await seats_of(db_session, rider)
    assert (first.route_id, first.status) == (old, AssignmentStatus.ENDED)


async def test_a_seat_holders_edit_must_fit_both_routes_while_moving(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    old = await add_route(db_session, trip, status=RouteStatus.ACTIVE, serves=(trip.megenagna,))
    new = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, old, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))  # fmt: skip
    await add_seat(db_session, new, rider, status=AssignmentStatus.ACTIVE)
    response = await api.patch(
        path(rider), json={"home_area_id": str(trip.megenagna)}, headers=signed_in(member)
    )
    assert [error["field"] for error in response.json()["errors"]] == ["/home_area_id"]


async def test_one_move_at_a_time(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """A second move while one is planned is refused (it would leave a month unplanned);
    undoing the first, by placing them back, lets the new one through."""
    first = await add_route(db_session, trip, status=RouteStatus.ACTIVE, code="R1")
    second = await add_route(db_session, trip, status=RouteStatus.ACTIVE, code="R2")
    third = await add_route(db_session, trip, status=RouteStatus.ACTIVE, code="R3")
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, first, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip
    assert (await _place(api, member, rider, second, start_date="2027-01-01")).status_code == 200
    again = await _place(api, member, rider, third, start_date="2027-02-01")
    assert (again.status_code, again.json()["code"]) == (409, "RIDER_MOVING")
    assert (await _place(api, member, rider, first)).status_code == 200  # undone
    response = await _place(api, member, rider, third, start_date="2027-02-01")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["seat"]["route_code"], body["seat"]["end_date"]) == ("R1", "2027-01-31")
    assert (body["next_seat"]["route_code"], body["next_seat"]["start_date"]) == (
        "R3",
        "2027-02-01",
    )
    statuses = {seat.route_id: seat.status for seat in await seats_of(db_session, rider)}
    assert statuses[second] is AssignmentStatus.ENDED


async def test_a_seat_still_to_come_is_replaced(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Not riding yet: the new placement takes its place, and it ends (it never rode)."""
    later = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    instead = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, later, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2027, 3, 1))  # fmt: skip
    response = await _place(api, member, rider, instead)
    assert (response.json()["seat"]["route_id"], response.json()["next_seat"]) == (
        str(instead),
        None,
    )


async def test_placing_back_on_the_route_they_are_leaving_undoes_the_end(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    seat = await add_seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                          start_date=date(2026, 11, 1), end_date=date(2026, 12, 31))  # fmt: skip
    response = await _place(api, member, rider, route)
    assert response.status_code == 200, response.json()
    assert response.json()["seat"]["end_date"] is None
    entries = await audit_entries(db_session, seat)
    assert [(entry.before, entry.after) for entry in entries] == [
        ({"end_date": "2026-12-31"}, {"end_date": None})
    ]


async def test_undoing_an_end_needs_the_seat_still_free(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """January went to someone else meanwhile."""
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE, seats=1,
                            launch_minimum=1)  # fmt: skip
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1), end_date=date(2026, 12, 31))  # fmt: skip
    await add_seat(db_session, route, await add_rider(db_session, trip),
                   status=AssignmentStatus.ACTIVE, start_date=date(2027, 1, 1))  # fmt: skip
    response = await _place(api, member, rider, route)
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_FULL")


async def test_placing_back_cancels_a_move_still_to_come(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    old = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    new = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, old, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))  # fmt: skip
    await add_seat(db_session, new, rider, status=AssignmentStatus.ACTIVE)
    response = await _place(api, member, rider, old)
    assert response.status_code == 200, response.json()
    assert (response.json()["seat"]["route_id"], response.json()["seat"]["end_date"]) == (
        str(old),
        None,
    )
    assert response.json()["next_seat"] is None


async def test_someone_riding_is_not_moved_to_a_forming_route(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """It may launch late or never: they'd be left without a seat."""
    running = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    forming = await add_route(db_session, trip)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, running, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip
    response = await _place(api, member, rider, forming)
    assert [error["field"] for error in response.json()["errors"]] == ["/route_id"]


@pytest.mark.parametrize(("name", "own"), [("Rider Who Asked", True), ("Her Brother", False)])
async def test_a_request_staff_recorded_is_theirs_by_phone_and_name(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    name: str,
    own: bool,
) -> None:
    """A relative asking with the same phone is someone else, waiting ahead of them."""
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip, full_name="Rider Who Asked", phone="+251911234567")
    await _request(db_session, route, rider_name=name.upper(), contact_phone="+251911234567")
    response = await _place(api, member, rider, route)
    assert response.status_code == (200 if own else 409), response.json()


# Ending


async def test_an_adult_leaves_at_this_months_end_by_default(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    seat = await add_seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                          start_date=date(2026, 11, 1))  # fmt: skip
    response = await _end(api, member, rider)
    assert response.status_code == 200, response.json()
    assert response.json()["seat"]["end_date"] == "2026-11-30"  # the seat stays empty till then
    (entry,) = await audit_entries(db_session, seat)
    assert (entry.action, entry.before, entry.after) == (
        "route_assignment.changed",
        {"end_date": None},
        {"end_date": "2026-11-30"},
    )


@pytest.mark.parametrize(
    ("end", "ok"),
    [("2026-12-31", True), ("2026-12-15", False), ("2026-10-31", False), ("soon", False)],
)
async def test_an_adult_leaves_at_a_coming_months_end(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    end: str,
    ok: bool,
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip
    response = await _end(api, member, rider, end_date=end)
    if ok:
        assert response.json()["seat"]["end_date"] == end
    else:
        assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
        assert [error["field"] for error in response.json()["errors"]] == ["/end_date"]


async def test_a_reservation_ends_at_once(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, await add_route(db_session, trip), rider)
    response = await _end(api, member, rider)
    assert (response.status_code, response.json()["seat"]) == (200, None)
    (seat,) = await seats_of(db_session, rider)
    assert seat.status is AssignmentStatus.ENDED


async def test_ending_also_ends_a_seat_still_to_start(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    old = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    new = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, old, rider, status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))  # fmt: skip
    await add_seat(db_session, new, rider, status=AssignmentStatus.ACTIVE)
    response = await _end(api, member, rider)
    assert response.json()["next_seat"] is None
    _, later = await seats_of(db_session, rider)
    assert later.status is AssignmentStatus.ENDED  # it never rode


async def test_ending_without_a_seat_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await add_rider(db_session, trip)
    response = await _end(api, member, rider)
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_HAS_NO_SEAT")


# Shared: keys, unknown riders, bodies, deletes, failures


@pytest.mark.parametrize("ending", [False, True])
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    member: uuid.UUID,
    ending: bool,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    rider = uuid.UUID(int=1)
    if ending:
        response = await api.post(end_path(rider), json={}, headers=with_key(member))
    else:
        response = await api.put(seat_path(rider), json={"route_id": str(rider)},
                                 headers=with_key(member))  # fmt: skip
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


@pytest.mark.parametrize("ending", [False, True])
async def test_retries_replay_and_keys_are_required(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, ending: bool
) -> None:
    route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    rider = await add_rider(db_session, trip)
    if ending:
        await add_seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                       start_date=date(2026, 11, 1))  # fmt: skip
    url, body = (end_path(rider), {}) if ending else (seat_path(rider), {"route_id": str(route)})
    method = "POST" if ending else "PUT"
    headers = with_key(member)
    first = await api.request(method, url, json=body, headers=headers)
    again = await api.request(method, url, json=body, headers=headers)
    assert (first.status_code, again.status_code, again.json()) == (200, 200, first.json())
    keyless = await api.request(method, url, json=body, headers=signed_in(member))
    assert (keyless.status_code, keyless.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")


async def test_an_unknown_or_deleted_rider_is_404(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip)
    gone = await add_rider(db_session, trip, deleted_at=datetime.now(UTC))
    for rider in (uuid.uuid4(), gone):
        placed = await _place(api, member, rider, route)
        ended = await _end(api, member, rider)
        assert (placed.status_code, ended.status_code) == (404, 404)


@pytest.mark.parametrize(
    "body",
    [{}, {"route_id": "r1"}, {"route_id": str(uuid.UUID(int=1)), "seats": 2},
     {"route_id": str(uuid.UUID(int=1)), "skip_reason": "x" * 101},
     {"route_id": str(uuid.UUID(int=1)), "skip_reason": "   "}],
    ids=str,
)  # fmt: skip
async def test_a_bad_placement_body_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, body: object
) -> None:
    rider = await add_rider(db_session, trip)
    response = await api.put(seat_path(rider), json=body, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_a_bad_end_body_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await add_rider(db_session, trip)
    response = await api.post(end_path(rider), json={"when": "now"}, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_a_rider_with_a_seat_still_to_start_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    admin = (await add_admin(db_session)).id
    rider = await add_rider(db_session, trip)
    await add_seat(db_session, await add_route(db_session, trip), rider,
                   start_date=date(2027, 3, 1))  # fmt: skip
    response = await api.delete(path(rider), headers=signed_in(admin))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_HAS_SEAT")
