"""GZ-034: staff manage riders.

Staff list (filters: type, seat, route, name), read, record (adults from a phone call),
look up by phone (in the body: phones never go in URLs) and edit riders; admins delete
them (soft), never while they hold a seat. Edits of a rider holding a seat must still fit
their route. Names and phones are personal data: redacted in the audit log, masked in
lists.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.riders.models import Rider
from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.factories import AreaFactory, RiderFactory, RouteRequestFactory
from tests.registrations.support import Trip, add_route, add_trip
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key

RIDERS = "/v1/riders"
PHONE = "+251911234567"


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


def _new(trip: Trip, **changes: object) -> dict[str, object]:
    return {
        "full_name": "Abebe Kebede",
        "phone": "0911 23 45 67",
        "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico),
        "plan": "round_trip",
        **changes,
    }


async def _rider(session: AsyncSession, trip: Trip, **values: Any) -> Rider:  # noqa: ANN401
    async with session.begin():
        return await RiderFactory().create(
            session,
            **{"home_area_id": trip.bole, "destination_area_id": trip.mexico, **values},
        )


async def _seat(
    session: AsyncSession,
    route: uuid.UUID,
    rider: uuid.UUID,
    **values: Any,  # noqa: ANN401
) -> None:
    async with session.begin():
        session.add(
            RouteAssignment(
                **{
                    "route_id": route,
                    "rider_id": rider,
                    "start_date": date(2026, 12, 1),
                    "status": AssignmentStatus.RESERVED,
                    **values,
                }
            )
        )


def _path(rider_id: uuid.UUID | str) -> str:
    return f"{RIDERS}/{rider_id}"


# Recording a rider


async def test_staff_record_an_adult_from_a_call(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    response = await api.post(
        RIDERS, json=_new(trip, saturday_addon=True), headers=with_key(member)
    )
    assert response.status_code == 201, response.json()
    created = response.json()
    assert created == {
        "id": created["id"],
        "rider_type": "adult",
        "full_name": "Abebe Kebede",
        "phone": PHONE,  # one spelling
        "home_area": {"id": str(trip.bole), "name": "Bole"},
        "destination_area": {"id": str(trip.mexico), "name": "Mexico"},
        "plan": "round_trip",
        "saturday_addon": True,
        "source": "staff",
        "seat": None,
        "open_requests": 0,
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }
    assert response.headers["Location"] == _path(created["id"])
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = await audit_entries(db_session, uuid.UUID(created["id"]))
    assert (entry.action, entry.actor_staff_id, entry.before) == ("rider.created", member, None)
    assert entry.after == {
        "rider_type": "adult",
        "full_name": REDACTED,  # personal data
        "phone": REDACTED,
        "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico),
        "plan": "round_trip",
        "saturday_addon": True,
        "source": "staff",
        "deleted_at": None,
    }


async def test_a_retried_record_replays(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    headers = with_key(member)
    first = await api.post(RIDERS, json=_new(trip), headers=headers)
    again = await api.post(RIDERS, json=_new(trip), headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    async with db_session.begin():
        assert len((await db_session.scalars(select(Rider))).all()) == 1


async def test_recording_needs_an_idempotency_key(
    api: httpx.AsyncClient, trip: Trip, member: uuid.UUID
) -> None:
    response = await api.post(RIDERS, json=_new(trip), headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")


async def test_one_live_adult_per_phone(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    await _rider(db_session, trip, phone=PHONE)
    response = await api.post(RIDERS, json=_new(trip), headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_EXISTS")
    await _rider(db_session, trip, phone="+251922334455", deleted_at=datetime.now(UTC))
    other = await api.post(RIDERS, json=_new(trip, phone="+251922334455"), headers=with_key(member))
    assert other.status_code == 201  # a deleted rider's phone is free


@pytest.mark.parametrize(
    "changes",
    [
        {"full_name": "   "},
        {"full_name": "x" * 101},
        {"phone": "0811234567"},
        {"phone": None},
        {"plan": "weekends"},
        {"saturday_addon": "yes"},
        {"home_area_id": "bole"},
        {"rider_type": "corporate"},  # company riders come with GZ-035
        {"source": "app"},
        {"destination_area_id": "SAME"},
    ],
    ids=str,
)
async def test_a_bad_new_rider_is_422(
    api: httpx.AsyncClient, trip: Trip, member: uuid.UUID, changes: dict[str, object]
) -> None:
    if changes.get("destination_area_id") == "SAME":
        changes = {"destination_area_id": str(trip.bole)}
    body = {k: v for k, v in _new(trip, **changes).items() if v is not None}
    response = await api.post(RIDERS, json=body, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("field", ["home_area_id", "destination_area_id"])
async def test_a_new_riders_areas_must_be_live(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, field: str
) -> None:
    async with db_session.begin():
        gone = await AreaFactory().create(db_session, deleted_at=datetime.now(UTC))
    for area in (uuid.uuid4(), gone.id):
        response = await api.post(
            RIDERS, json=_new(trip, **{field: str(area)}), headers=with_key(member)
        )
        assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
        assert [error["field"] for error in response.json()["errors"]] == [f"/{field}"]


# Reading


async def test_one_rider_with_their_seat_and_open_requests(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip, phone=PHONE, full_name="Abebe Kebede")
    route = await add_route(db_session, trip, code="BOLE-MEX-01")
    await _seat(db_session, route, rider.id)
    async with db_session.begin():
        await RouteRequestFactory().create(db_session, rider_id=rider.id)
        await RouteRequestFactory().create(db_session, rider_id=rider.id, status="declined",
                                           decline_reason="Too far.")  # fmt: skip
    response = await api.get(_path(rider.id), headers=signed_in(member))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["phone"] == PHONE  # in full on the detail
    assert body["seat"] == {
        "route_id": str(route),
        "route_code": "BOLE-MEX-01",
        "status": "reserved",
        "start_date": "2026-12-01",
        "end_date": None,
    }
    assert body["open_requests"] == 1


async def test_a_seat_that_has_ended_is_not_shown(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip)
    route = await add_route(db_session, trip)
    await _seat(db_session, route, rider.id, status=AssignmentStatus.ENDED)
    left = await add_route(db_session, trip)
    await _seat(db_session, left, rider.id, status=AssignmentStatus.ACTIVE,
                start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))  # fmt: skip
    response = await api.get(_path(rider.id), headers=signed_in(member))
    assert response.json()["seat"] is None


async def test_an_unknown_or_deleted_rider_is_404(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    gone = await _rider(db_session, trip, deleted_at=datetime.now(UTC))
    for rider_id in (uuid.uuid4(), gone.id):
        response = await api.get(_path(rider_id), headers=signed_in(member))
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_the_list_masks_phones_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    older = await _rider(
        db_session, trip, phone=PHONE, created_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    newer = await _rider(db_session, trip, created_at=datetime(2026, 10, 2, tzinfo=UTC))
    await _rider(db_session, trip, deleted_at=datetime.now(UTC))
    response = await api.get(RIDERS, headers=signed_in(member))
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(newer.id), str(older.id)]
    assert items[1]["phone_masked"] == "+2519****4567"
    assert "phone" not in items[1]
    assert set(items[1]) == {
        "id", "rider_type", "full_name", "phone_masked", "home_area", "destination_area",
        "plan", "saturday_addon", "source", "seat", "open_requests", "created_at",
        "updated_at",
    }  # fmt: skip


async def test_the_list_filters(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route, other = await add_route(db_session, trip), await add_route(db_session, trip)
    reserved = await _rider(db_session, trip, full_name="Abebe Kebede")
    await _seat(db_session, route, reserved.id)
    riding = await _rider(db_session, trip, full_name="Almaz Tesfaye")
    await _seat(db_session, other, riding.id, status=AssignmentStatus.ACTIVE,
                start_date=date(2026, 11, 1))  # fmt: skip
    none = await _rider(db_session, trip, full_name="Bekele Girma")

    async def ids(**params: str) -> set[str]:
        response = await api.get(RIDERS, params=params, headers=signed_in(member))
        assert response.status_code == 200, response.json()
        return {item["id"] for item in response.json()["items"]}

    assert await ids(seat="reserved") == {str(reserved.id)}
    assert await ids(seat="active") == {str(riding.id)}
    assert await ids(seat="none") == {str(none.id)}
    assert await ids(route_id=str(other)) == {str(riding.id)}
    assert await ids(q="a") == {str(reserved.id), str(riding.id)}  # names starting with it
    assert await ids(q="ALM") == {str(riding.id)}
    assert await ids(rider_type="adult") == {str(reserved.id), str(riding.id), str(none.id)}
    assert await ids(rider_type="corporate") == set()


async def test_the_list_comes_in_pages(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    for day in (1, 2, 3):
        await _rider(db_session, trip, created_at=datetime(2026, 10, day, tzinfo=UTC))
    first = (await api.get(RIDERS, params={"limit": "2"}, headers=signed_in(member))).json()
    second = (
        await api.get(RIDERS, params={"cursor": first["next_cursor"]}, headers=signed_in(member))
    ).json()
    assert (len(first["items"]), len(second["items"]), second["next_cursor"]) == (2, 1, None)


@pytest.mark.parametrize(
    "params",
    [{"seat": "maybe"}, {"rider_type": "child"}, {"route_id": "r1"}, {"phone": PHONE},
     {"limit": "0"}, {"q": "x" * 101}],
    ids=str,
)  # fmt: skip
async def test_bad_list_parameters_are_422(
    api: httpx.AsyncClient, member: uuid.UUID, params: dict[str, str]
) -> None:
    response = await api.get(RIDERS, params=params, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_staff_look_riders_up_by_phone(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """In the body, typed any usual way: a phone never goes in a URL or a log."""
    rider = await _rider(db_session, trip, phone=PHONE)
    await _rider(db_session, trip, phone="+251922334455", deleted_at=datetime.now(UTC))
    found = await api.post(
        f"{RIDERS}/lookup", json={"phone": "0911 234 567"}, headers=signed_in(member)
    )
    assert found.status_code == 200
    assert found.headers["Cache-Control"] == "no-store"
    assert [item["id"] for item in found.json()["items"]] == [str(rider.id)]
    missing = await api.post(
        f"{RIDERS}/lookup", json={"phone": "+251922334455"}, headers=signed_in(member)
    )
    assert missing.json()["items"] == []
    bad = await api.post(f"{RIDERS}/lookup", json={"phone": "12"}, headers=signed_in(member))
    assert (bad.status_code, bad.json()["code"]) == (422, "VALIDATION_FAILED")


# Editing


async def test_staff_edit_a_rider(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip, full_name="Abebe")
    change = {
        "full_name": "Abebe Kebede",
        "phone": "0922 33 44 55",
        "home_area_id": str(trip.megenagna),
        "destination_area_id": str(trip.summit),
        "plan": "evening",
        "saturday_addon": True,
    }
    response = await api.patch(_path(rider.id), json=change, headers=signed_in(member))
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["full_name"], body["phone"], body["plan"], body["saturday_addon"]) == (
        "Abebe Kebede",
        "+251922334455",
        "evening",
        True,
    )
    assert (body["home_area"]["name"], body["destination_area"]["name"]) == ("Megenagna", "Summit")
    (entry,) = await audit_entries(db_session, rider.id)
    assert entry.action == "rider.updated"
    assert entry.before == {
        "full_name": REDACTED, "phone": REDACTED, "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico), "plan": "round_trip", "saturday_addon": False,
    }  # fmt: skip


async def test_an_edit_that_changes_nothing_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip, plan="morning")
    response = await api.patch(_path(rider.id), json={"plan": "morning"}, headers=signed_in(member))
    assert response.status_code == 200
    assert await audit_entries(db_session, rider.id) == []


async def test_a_phone_another_rider_has_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    await _rider(db_session, trip, phone=PHONE)
    rider = await _rider(db_session, trip)
    response = await api.patch(_path(rider.id), json={"phone": PHONE}, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_EXISTS")


@pytest.mark.parametrize(
    "change",
    [{}, {"plan": None}, {"phone": "12"}, {"source": "app"}, {"rider_type": "corporate"},
     {"destination_area_id": "HOME"}, {"home_area_id": "DESTINATION"}],
    ids=str,
)  # fmt: skip
async def test_a_bad_edit_is_422(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    change: dict[str, object],
) -> None:
    """Home and destination stay different, whichever of them changes."""
    rider = await _rider(db_session, trip)
    if change.get("destination_area_id") == "HOME":
        change = {"destination_area_id": str(trip.bole)}
    if change.get("home_area_id") == "DESTINATION":
        change = {"home_area_id": str(trip.mexico)}
    response = await api.patch(_path(rider.id), json=change, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_an_edit_to_a_dead_area_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip)
    response = await api.patch(
        _path(rider.id), json={"home_area_id": str(uuid.uuid4())}, headers=signed_in(member)
    )
    assert [error["field"] for error in response.json()["errors"]] == ["/home_area_id"]


async def test_editing_an_unknown_or_deleted_rider_is_404(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    gone = await _rider(db_session, trip, deleted_at=datetime.now(UTC))
    for rider_id in (uuid.uuid4(), gone.id):
        response = await api.patch(_path(rider_id), json={"plan": "evening"},
                                   headers=signed_in(member))  # fmt: skip
        assert response.status_code == 404


async def test_a_seat_holders_edit_must_still_fit_their_route(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Boarding where the route passes, going to its B, Saturday only if it offers it."""
    route = await add_route(db_session, trip, serves=(trip.megenagna,))
    rider = (await _rider(db_session, trip)).id
    await _seat(db_session, route, rider)

    async def edit(**change: object) -> httpx.Response:
        return await api.patch(_path(rider), json=change, headers=signed_in(member))

    moved = await edit(home_area_id=str(trip.megenagna))
    assert moved.status_code == 200  # the route passes Megenagna
    for change, field in (
        ({"home_area_id": str(trip.summit)}, "/home_area_id"),
        ({"destination_area_id": str(trip.summit)}, "/destination_area_id"),
        ({"saturday_addon": True}, "/saturday_addon"),
    ):
        response = await edit(**change)
        assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
        assert [error["field"] for error in response.json()["errors"]] == [field]


async def test_a_route_offering_saturday_takes_the_add_on(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, price_saturday=600)
    rider = (await _rider(db_session, trip)).id
    await _seat(db_session, route, rider)
    response = await api.patch(
        _path(rider), json={"saturday_addon": True}, headers=signed_in(member)
    )
    assert (response.status_code, response.json()["saturday_addon"]) == (200, True)


@pytest.mark.parametrize(
    ("home", "destination", "blamed"),
    [
        ("megenagna", "summit", "/destination_area_id"),  # boards on the way, wrong B
        ("summit", "mexico", "/home_area_id"),  # the route doesn't pass Summit
    ],
)
async def test_both_areas_changing_blames_the_one_that_breaks_it(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    home: str,
    destination: str,
    blamed: str,
) -> None:
    route = await add_route(db_session, trip, serves=(trip.megenagna,))
    rider = (await _rider(db_session, trip)).id
    await _seat(db_session, route, rider)
    change = {
        "home_area_id": str(getattr(trip, home)),
        "destination_area_id": str(getattr(trip, destination)),
    }
    response = await api.patch(_path(rider), json=change, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == [blamed]


async def test_a_seat_holder_may_change_what_the_route_does_not_care_about(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip)
    rider = (await _rider(db_session, trip)).id
    await _seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                start_date=date(2026, 11, 1))  # fmt: skip
    response = await api.patch(
        _path(rider),
        json={"full_name": "Abebe Kebede", "plan": "morning", "phone": "+251922334455"},
        headers=signed_in(member),
    )
    assert response.status_code == 200
    moved = await api.patch(
        _path(rider), json={"home_area_id": str(trip.summit)}, headers=signed_in(member)
    )
    assert moved.status_code == 422  # an active seat restricts edits like a reserved one


async def test_a_seat_that_ended_restricts_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip)
    rider = (await _rider(db_session, trip)).id
    await _seat(db_session, route, rider, status=AssignmentStatus.ACTIVE,
                start_date=date(2026, 10, 1), end_date=date(2026, 10, 31))  # fmt: skip
    response = await api.patch(
        _path(rider),
        json={"home_area_id": str(trip.summit), "saturday_addon": True},
        headers=signed_in(member),
    )
    assert response.status_code == 200


# Deleting


async def test_admins_delete_a_rider_without_a_seat(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    admin = (await add_admin(db_session)).id
    rider = (await _rider(db_session, trip)).id
    route = await add_route(db_session, trip)
    await _seat(db_session, route, rider, status=AssignmentStatus.ENDED)
    response = await api.delete(_path(rider), headers=signed_in(admin))
    assert response.status_code == 204
    assert (await api.get(_path(rider), headers=signed_in(admin))).status_code == 404
    entries = await audit_entries(db_session, rider)
    assert [entry.action for entry in entries] == ["rider.deleted"]


async def test_a_rider_holding_a_seat_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """End the seat first (GZ-035): nobody loses a place by a delete."""
    admin = (await add_admin(db_session)).id
    rider = await _rider(db_session, trip)
    await _seat(db_session, await add_route(db_session, trip), rider.id)
    response = await api.delete(_path(rider.id), headers=signed_in(admin))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_HAS_SEAT")


async def test_staff_cannot_delete_riders(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    rider = await _rider(db_session, trip)
    response = await api.delete(_path(rider.id), headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")


# Failures and logs


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", RIDERS),
        ("GET", _path(uuid.UUID(int=1))),
        ("POST", f"{RIDERS}/lookup"),
        ("POST", RIDERS),
        ("PATCH", _path(uuid.UUID(int=1))),
        ("DELETE", _path(uuid.UUID(int=1))),
    ],
)
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    unreachable_sessions: AsyncSession,
    trip: Trip,
    method: str,
    path: str,
) -> None:
    admin = (await add_admin(db_session)).id  # may call every one of them

    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    body = _new(trip) if method == "POST" and path == RIDERS else {"phone": PHONE}
    if method == "PATCH":
        body = {"plan": "morning"}
    response = await api.request(method, path, json=body, headers=with_key(admin))
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
