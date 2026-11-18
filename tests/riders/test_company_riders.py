"""GZ-035: company riders ("today" is 15 November 2026).

Staff record them for their company: name, phone, company and where they board (no
destination: they ride to the route's B; no plan or Saturday: the company is billed). A
phone appears once per company. They ride only their company's corporate route, active or
paused (GZ-078: not a draft), starting and leaving on any day (today by default).
"""

import uuid
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.companies.models import Company
from gize_api.routes.models import AssignmentStatus, RouteStatus, RouteType
from tests.factories import CompanyFactory
from tests.registrations.support import Trip, add_route, add_trip
from tests.riders.support import (
    RIDERS,
    add_seat,
    end_path,
    path,
    seat_path,
    seats_of,
)
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key

PHONE = "+251911234567"


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


@pytest.fixture
async def company(db_session: AsyncSession) -> uuid.UUID:
    async with db_session.begin():
        return (await CompanyFactory().create(db_session, name="Acme Ethiopia")).id


def _new(trip: Trip, company: uuid.UUID, **changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "rider_type": "corporate",
        "full_name": "Hanna Bekele",
        "phone": PHONE,
        "company_id": str(company),
        "home_area_id": str(trip.bole),
        **changes,
    }
    return {name: value for name, value in body.items() if value is not None}


async def _corporate_route(
    session: AsyncSession,
    trip: Trip,
    company: uuid.UUID,
    *,
    serves: tuple[uuid.UUID, ...] = (),
    **values: object,
) -> uuid.UUID:
    return await add_route(
        session,
        trip,
        serves=serves,
        **{
            "route_type": RouteType.CORPORATE,
            "company_id": company,
            "status": RouteStatus.ACTIVE,
            "launch_minimum": None,
            "price_round_trip": None,
            "price_one_way": None,
            **values,
        },
    )


async def _recorded(
    api: httpx.AsyncClient, staff: uuid.UUID, trip: Trip, company: uuid.UUID, **changes: object
) -> str:
    response = await api.post(RIDERS, json=_new(trip, company, **changes), headers=with_key(staff))
    assert response.status_code == 201, response.json()
    return str(response.json()["id"])


# Recording


async def test_staff_record_a_company_rider(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    response = await api.post(RIDERS, json=_new(trip, company), headers=with_key(member))
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["rider_type"], body["company"]) == (
        "corporate",
        {"id": str(company), "name": "Acme Ethiopia"},
    )
    assert body["home_area"] == {"id": str(trip.bole), "name": "Bole"}
    assert (body["destination_area"], body["plan"], body["saturday_addon"]) == (None, None, False)
    (entry,) = await audit_entries(db_session, uuid.UUID(body["id"]))
    assert entry.after is not None
    assert (entry.after["rider_type"], entry.after["company_id"]) == ("corporate", str(company))


async def test_adults_have_no_company(
    api: httpx.AsyncClient, trip: Trip, member: uuid.UUID
) -> None:
    body = {
        "full_name": "Abebe Kebede", "phone": PHONE, "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico), "plan": "round_trip",
    }  # fmt: skip
    response = await api.post(RIDERS, json=body, headers=with_key(member))
    assert response.json()["company"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": None},
        {"home_area_id": None},
        {"phone": None},
        {"destination_area_id": "MEXICO"},
        {"plan": "round_trip"},
        {"saturday_addon": True},
    ],
    ids=str,
)
async def test_a_bad_company_rider_is_422(
    api: httpx.AsyncClient,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
    changes: dict[str, object],
) -> None:
    if changes.get("destination_area_id") == "MEXICO":
        changes = {"destination_area_id": str(trip.mexico)}
    response = await api.post(RIDERS, json=_new(trip, company, **changes), headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("problem", ["unknown", "deleted", "inactive"])
async def test_the_company_must_be_live_and_active(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, problem: str
) -> None:
    company = uuid.uuid4()
    if problem != "unknown":
        async with db_session.begin():
            company = (
                await CompanyFactory().create(
                    db_session,
                    **(
                        {"deleted_at": datetime.now(UTC)}
                        if problem == "deleted"
                        else {"is_active": False}
                    ),
                )
            ).id
    response = await api.post(RIDERS, json=_new(trip, company), headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/company_id"]


async def test_a_phone_appears_once_per_company(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    await _recorded(api, member, trip, company)
    again = await api.post(RIDERS, json=_new(trip, company), headers=with_key(member))
    assert (again.status_code, again.json()["code"]) == (409, "RIDER_EXISTS")
    async with db_session.begin():
        other = (await CompanyFactory().create(db_session)).id
    await _recorded(api, member, trip, other)  # another employer
    adult = {
        "full_name": "Hanna Bekele", "phone": PHONE, "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico), "plan": "round_trip",
    }  # fmt: skip
    response = await api.post(RIDERS, json=adult, headers=with_key(member))
    assert response.status_code == 201  # and a subscription of their own


# Seats


async def test_a_company_rider_starts_today_on_their_companys_route(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    route = await _corporate_route(db_session, trip, company)
    rider = await _recorded(api, member, trip, company)
    response = await api.put(
        seat_path(rider), json={"route_id": str(route)}, headers=with_key(member)
    )
    assert response.status_code == 200, response.json()
    seat = response.json()["seat"]
    assert (seat["status"], seat["start_date"]) == ("active", "2026-11-15")


async def test_a_paused_company_route_still_takes_riders(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    """Company riders keep their seats through a pause, and new ones join it."""
    route = await _corporate_route(db_session, trip, company, status=RouteStatus.PAUSED)
    rider = await _recorded(api, member, trip, company)
    response = await api.put(
        seat_path(rider),
        json={"route_id": str(route), "start_date": "2026-11-20"},  # any day
        headers=with_key(member),
    )
    assert response.status_code == 200, response.json()
    seat = response.json()["seat"]
    assert (seat["status"], seat["start_date"]) == ("active", "2026-11-20")


async def test_a_draft_company_route_takes_no_riders(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    """GZ-078 (owner decision, 2026-11-18; before, a draft took riders, each with an active
    seat on a route that wasn't running): staff start the route first (it only needs an
    active company). The same 422 as adults on a route that doesn't take them."""
    route = await _corporate_route(db_session, trip, company, status=RouteStatus.DRAFT)
    rider = await _recorded(api, member, trip, company)
    response = await api.put(
        seat_path(rider), json={"route_id": str(route)}, headers=with_key(member)
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/route_id"]
    assert await seats_of(db_session, uuid.UUID(rider)) == []


@pytest.mark.parametrize("problem", ["other company", "public", "past", "elsewhere"])
async def test_a_company_rider_rides_only_their_companys_route(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
    problem: str,
) -> None:
    body: dict[str, str] = {}
    field = "/route_id"
    if problem == "other company":
        async with db_session.begin():
            other = (await CompanyFactory().create(db_session)).id
        route = await _corporate_route(db_session, trip, other)
    elif problem == "public":
        route = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    elif problem == "past":
        route = await _corporate_route(db_session, trip, company)
        body, field = {"start_date": "2026-11-14"}, "/start_date"
    else:  # boards somewhere the route doesn't pass
        route = await _corporate_route(db_session, trip, company)
    home = trip.summit if problem == "elsewhere" else trip.bole
    rider = await _recorded(api, member, trip, company, home_area_id=str(home))
    response = await api.put(
        seat_path(rider), json={"route_id": str(route), **body}, headers=with_key(member)
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == [field]


async def test_a_company_rider_leaves_on_any_day_today_by_default(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    route = await _corporate_route(db_session, trip, company)
    rider = await _recorded(api, member, trip, company)
    await add_seat(db_session, route, uuid.UUID(rider), status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip
    chosen = await api.post(
        end_path(rider), json={"end_date": "2026-11-20"}, headers=with_key(member)
    )
    assert chosen.json()["seat"]["end_date"] == "2026-11-20"
    today = await api.post(end_path(rider), json={}, headers=with_key(member))
    assert today.json()["seat"]["end_date"] == "2026-11-15"
    past = await api.post(end_path(rider), json={"end_date": "2026-11-14"},
                          headers=with_key(member))  # fmt: skip
    assert [error["field"] for error in past.json()["errors"]] == ["/end_date"]


async def test_a_company_rider_starting_later_and_ended_today_never_rides(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    route = await _corporate_route(db_session, trip, company)
    rider = await _recorded(api, member, trip, company)
    await add_seat(db_session, route, uuid.UUID(rider), status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 20))  # fmt: skip
    response = await api.post(end_path(rider), json={}, headers=with_key(member))
    assert response.json()["seat"] is None
    (seat,) = await seats_of(db_session, uuid.UUID(rider))
    assert seat.status is AssignmentStatus.ENDED


async def test_an_inactive_companys_riders_are_not_placed(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    """Its routes can't run (GZ-024): the same 409 as launching one. Since GZ-078 the API
    can't get here: only an active or paused route takes company riders, a deactivation is
    refused while one runs (COMPANY_IN_USE), and its race with a start is refused too
    (tests/routes/test_concurrency.py). The check stays as a backstop for a direct data
    change, set here directly."""
    route = await _corporate_route(db_session, trip, company)
    rider = await _recorded(api, member, trip, company)
    async with db_session.begin():
        (await db_session.get_one(Company, company)).is_active = False
    response = await api.put(
        seat_path(rider), json={"route_id": str(route)}, headers=with_key(member)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_NOT_READY")


# Edits, lists, deletes


@pytest.mark.parametrize(
    "change", [{"destination_area_id": "MEXICO"}, {"plan": "morning"}, {"saturday_addon": True}],
    ids=str,
)  # fmt: skip
async def test_a_company_rider_has_no_subscription_to_edit(
    api: httpx.AsyncClient,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
    change: dict[str, object],
) -> None:
    rider = await _recorded(api, member, trip, company)
    if change.get("destination_area_id") == "MEXICO":
        change = {"destination_area_id": str(trip.mexico)}
    response = await api.patch(path(rider), json=change, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_a_company_riders_boarding_stays_on_their_route(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    route = await _corporate_route(db_session, trip, company, serves=(trip.megenagna,))
    rider = await _recorded(api, member, trip, company)
    await add_seat(db_session, route, uuid.UUID(rider), status=AssignmentStatus.ACTIVE,
                   start_date=date(2026, 11, 1))  # fmt: skip

    async def board_at(area: uuid.UUID) -> int:
        response = await api.patch(
            path(rider), json={"home_area_id": str(area)}, headers=signed_in(member)
        )
        return response.status_code

    assert await board_at(trip.megenagna) == 200
    assert await board_at(trip.summit) == 422
    phone = await api.patch(path(rider), json={"phone": "+251922334455"},
                            headers=signed_in(member))  # fmt: skip
    assert phone.status_code == 200


async def test_a_phone_taken_at_the_company_is_409_on_edit(
    api: httpx.AsyncClient, trip: Trip, member: uuid.UUID, company: uuid.UUID
) -> None:
    await _recorded(api, member, trip, company)
    rider = await _recorded(api, member, trip, company, phone="+251922334455")
    response = await api.patch(path(rider), json={"phone": PHONE}, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_EXISTS")


async def test_company_riders_are_listed_by_type(
    api: httpx.AsyncClient, trip: Trip, member: uuid.UUID, company: uuid.UUID
) -> None:
    rider = await _recorded(api, member, trip, company)
    response = await api.get(RIDERS, params={"rider_type": "corporate"},
                             headers=signed_in(member))  # fmt: skip
    assert [item["id"] for item in response.json()["items"]] == [rider]
    assert response.json()["items"][0]["company"]["name"] == "Acme Ethiopia"


async def test_a_company_with_riders_is_not_deleted(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    company: uuid.UUID,
) -> None:
    admin = (await add_admin(db_session)).id
    rider = await _recorded(api, member, trip, company)
    refused = await api.delete(f"/v1/companies/{company}", headers=signed_in(admin))
    assert (refused.status_code, refused.json()["code"]) == (409, "COMPANY_IN_USE")
    assert (await api.delete(path(rider), headers=signed_in(admin))).status_code == 204
    deleted = await api.delete(f"/v1/companies/{company}", headers=signed_in(admin))
    assert deleted.status_code == 204
