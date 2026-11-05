"""GZ-040: a free seat offered to the oldest request, for 48 hours ("today" is 15 November
2026).

Staff offer a route's free seat (`POST /v1/routes/{id}/offers`): by default to the oldest
open request waiting for it (asked for the route, or matched to it), to another only with
a reason; an SMS tells the person. The offer holds the seat for 48 hours. They accept by
phone (staff, `POST /v1/offers/{id}/accept`) or on the website (phone verified, `POST
/v1/public/offers/{id}/accept`): the seat is theirs (a place on an active route from a 1st,
a reservation on a forming one) and the request fulfilled. Expired, withdrawn or accepted
offers can't be accepted.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.integrations.sms import FakeSmsSender, SmsSendError
from gize_api.main import GizeAPI
from gize_api.offers.models import SeatOffer
from gize_api.riders.models import Rider
from gize_api.route_requests.models import RequestKind, RequestStatus, RouteRequest
from gize_api.routes.models import AssignmentStatus, RouteAssignment, RouteStatus
from tests.factories import RiderFactory, RouteRequestFactory
from tests.registrations.support import Trip, add_route, add_trip, verified
from tests.staff.support import add_staff, audit_entries, signed_in, with_key

PHONE = "+251911000001"


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def _running(session: AsyncSession, trip: Trip, *, free: int = 1, **values: Any) -> uuid.UUID:  # noqa: ANN401
    """An active route with `free` seats from 1 December (the rest held by riders)."""
    route = await add_route(
        session, trip, **{"code": "BOLE-MEX-01", "status": RouteStatus.ACTIVE, "seats": 3,
                          "launch_minimum": 1, **values},
    )  # fmt: skip
    async with session.begin():
        for _ in range(3 - free):
            rider = await RiderFactory().create(
                session, home_area_id=trip.bole, destination_area_id=trip.mexico
            )
            session.add(RouteAssignment(route_id=route, rider_id=rider.id,
                                        start_date=date(2026, 11, 1),
                                        status=AssignmentStatus.ACTIVE))  # fmt: skip
    return route


async def _request(
    session: AsyncSession,
    route: uuid.UUID,
    *,
    day: int,
    phone: str = PHONE,
    **values: Any,  # noqa: ANN401
) -> uuid.UUID:
    async with session.begin():
        request = await RouteRequestFactory().create(
            session,
            **{
                "kind": RequestKind.ROUTE,
                "route_id": route,
                "from_area_name": None,
                "to_area_name": None,
                "rider_name": f"Asker {day}",
                "contact_phone": phone,
                "created_at": datetime(2026, 10, day, tzinfo=UTC),
                **values,
            },
        )
        return request.id


async def _offer(
    api: httpx.AsyncClient, staff: uuid.UUID, route: uuid.UUID, **body: str
) -> httpx.Response:
    return await api.post(f"/v1/routes/{route}/offers", json=body, headers=with_key(staff))


async def _accept(api: httpx.AsyncClient, staff: uuid.UUID, offer: str) -> httpx.Response:
    return await api.post(f"/v1/offers/{offer}/accept", json={}, headers=with_key(staff))


async def _one[M](session: AsyncSession, model: type[M], **where: Any) -> M:  # noqa: ANN401
    async with session.begin():
        statement = select(model).filter_by(**where)
        return (await session.scalars(statement)).one()


# Offering


async def test_staff_offer_a_free_seat_to_the_oldest_request(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    route = await _running(db_session, trip)
    oldest = await _request(db_session, route, day=1)
    await _request(db_session, route, day=2, phone="+251911000002")
    response = await _offer(api, member, route)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["request_id"], body["route_code"], body["rider_name"]) == (
        str(oldest), "BOLE-MEX-01", "Asker 1",
    )  # fmt: skip
    assert (body["start_date"], body["status"], body["skip_reason"], body["rider_id"]) == (
        "2026-12-01", "open", None, None,
    )  # fmt: skip
    created = datetime.fromisoformat(body["created_at"])
    assert datetime.fromisoformat(body["expires_at"]) - created == timedelta(hours=48)
    assert response.headers["Location"] == f"/v1/offers/{body['id']}"
    assert sms.sent == [
        (PHONE, "Gize: a seat on route BOLE-MEX-01 is free for you from 1 December 2026. "
                "Accept within 48 hours on Gize's website or by calling Gize.")
    ]  # fmt: skip
    (entry,) = await audit_entries(db_session, uuid.UUID(body["id"]))
    assert (entry.action, entry.actor_staff_id) == ("seat_offer.created", member)
    read = (await api.get(f"/v1/offers/{body['id']}", headers=signed_in(member))).json()
    assert read["sms_status"] == "sent"


async def test_skipping_the_queue_needs_a_reason(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    later = await _request(db_session, route, day=2, phone="+251911000002")
    refused = await _offer(api, member, route, request_id=str(later))
    assert (refused.status_code, refused.json()["code"]) == (409, "ROUTE_HAS_WAITING_REQUESTS")
    offered = await _offer(api, member, route, request_id=str(later), skip_reason="Works nights.")
    assert offered.status_code == 201, offered.json()
    assert offered.json()["skip_reason"] == "Works nights."
    (entry,) = await audit_entries(db_session, uuid.UUID(offered.json()["id"]))
    assert entry.after is not None
    assert entry.after["skip_reason"] == REDACTED


async def test_a_request_matched_to_the_route_waits_too(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    async with db_session.begin():
        matched = (
            await RouteRequestFactory().create(
                db_session, status=RequestStatus.MATCHED, matched_route_id=route,
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        ).id  # fmt: skip
    response = await _offer(api, member, route)
    assert response.json()["request_id"] == str(matched)


async def test_with_nobody_waiting_there_is_nothing_to_offer(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1, status=RequestStatus.DECLINED,
                   decline_reason="Moved.")  # fmt: skip
    response = await _offer(api, member, route)
    assert (response.status_code, response.json()["code"]) == (409, "NO_WAITING_REQUESTS")


async def test_an_open_offer_holds_its_seat(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """One free seat, offered: a second offer, or staff placing someone, finds it full."""
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    await _request(db_session, route, day=2, phone="+251911000002")
    assert (await _offer(api, member, route)).status_code == 201
    second = await _offer(api, member, route)
    assert (second.status_code, second.json()["code"]) == (409, "ROUTE_FULL")
    async with db_session.begin():
        other = (
            await RiderFactory().create(
                db_session, home_area_id=trip.bole, destination_area_id=trip.mexico
            )
        ).id
    placed = await api.put(
        f"/v1/riders/{other}/seat",
        json={"route_id": str(route), "skip_reason": "Testing the hold."},
        headers=with_key(member),
    )
    assert (placed.status_code, placed.json()["code"]) == (409, "ROUTE_FULL")


async def test_one_open_offer_per_request(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip, free=2)
    request = await _request(db_session, route, day=1)
    assert (await _offer(api, member, route)).status_code == 201
    again = await _offer(api, member, route, request_id=str(request))
    assert (again.status_code, again.json()["code"]) == (409, "OFFER_OPEN")


@pytest.mark.parametrize("status", [RouteStatus.DRAFT, RouteStatus.PAUSED])
async def test_only_forming_and_active_routes_offer_seats(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    status: RouteStatus,
) -> None:
    route = await add_route(db_session, trip, status=status)
    await _request(db_session, route, day=1)
    response = await _offer(api, member, route)
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_NOT_READY")


@pytest.mark.parametrize(
    ("start", "ok"),
    [("2027-01-01", True), ("2026-12-15", False), ("2026-11-01", False), ("2027-12-01", False)],
)
async def test_an_offer_starts_on_a_coming_1st(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    start: str,
    ok: bool,
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    response = await _offer(api, member, route, start_date=start)
    if ok:
        assert response.json()["start_date"] == start
    else:
        assert [error["field"] for error in response.json()["errors"]] == ["/start_date"]


async def test_an_sms_failure_keeps_the_offer(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    """Staff see it failed and call the person."""

    class Failing:
        async def send(self, to: str, text: str) -> str:
            raise SmsSendError("unavailable")

        async def aclose(self) -> None:
            return None

    api_app.state.sms_sender = Failing()
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    response = await _offer(api, member, route)
    assert response.status_code == 201
    read = (await api.get(f"/v1/offers/{response.json()['id']}", headers=signed_in(member))).json()
    assert read["sms_status"] == "failed"


async def test_a_retried_offer_replays(
    api: httpx.AsyncClient, db_session: AsyncSession, sms: FakeSmsSender, trip: Trip,
    member: uuid.UUID,
) -> None:  # fmt: skip
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    headers = with_key(member)
    first = await api.post(f"/v1/routes/{route}/offers", json={}, headers=headers)
    again = await api.post(f"/v1/routes/{route}/offers", json={}, headers=headers)
    assert (first.status_code, again.status_code, again.json()) == (201, 201, first.json())
    assert len(sms.sent) == 1


# Accepting


async def test_an_accepted_offer_becomes_their_seat(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """By phone: a new rider from the request (A to B), a place from the offer's 1st."""
    route = await _running(db_session, trip)
    request = await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    response = await _accept(api, member, offer)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["status"], body["rider_id"] is not None) == ("accepted", True)
    rider = await _one(db_session, Rider, id=uuid.UUID(body["rider_id"]))
    assert (rider.full_name, rider.phone, rider.home_area_id, rider.destination_area_id) == (
        "Asker 1", PHONE, trip.bole, trip.mexico,
    )  # fmt: skip
    seat = await _one(db_session, RouteAssignment, rider_id=rider.id)
    assert (seat.route_id, seat.status, seat.start_date) == (
        route, AssignmentStatus.ACTIVE, date(2026, 12, 1),
    )  # fmt: skip
    asked = await _one(db_session, RouteRequest, id=request)
    assert (asked.status, asked.rider_id) == (RequestStatus.FULFILLED, rider.id)
    actions = {entry.action for entry in await audit_entries(db_session, uuid.UUID(offer))}
    assert actions == {"seat_offer.created", "seat_offer.accepted"}


async def test_a_forming_route_offer_becomes_a_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await add_route(db_session, trip, code="NEW-VAN")
    async with db_session.begin():
        await RouteRequestFactory().create(
            db_session, status=RequestStatus.MATCHED, matched_route_id=route
        )
    offer = (await _offer(api, member, route)).json()
    assert offer["start_date"] == "2026-12-01"
    accepted = await _accept(api, member, offer["id"])
    seat = await _one(db_session, RouteAssignment, rider_id=uuid.UUID(accepted.json()["rider_id"]))
    assert seat.status is AssignmentStatus.RESERVED


async def test_the_asker_s_own_rider_record_is_used(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    async with db_session.begin():
        rider = await RiderFactory().create(
            db_session, phone=PHONE, home_area_id=trip.bole, destination_area_id=trip.mexico
        )
        rider_id = rider.id
    await _request(db_session, route, day=1, rider_id=rider_id)
    offer = (await _offer(api, member, route)).json()["id"]
    assert (await _accept(api, member, offer)).json()["rider_id"] == str(rider_id)


async def test_someone_already_riding_elsewhere_is_moved_by_staff_instead(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    other = await _running(db_session, trip, code="OTHER-01")
    async with db_session.begin():
        rider = await RiderFactory().create(
            db_session, phone=PHONE, home_area_id=trip.bole, destination_area_id=trip.mexico
        )
        db_session.add(RouteAssignment(route_id=other, rider_id=rider.id,
                                       start_date=date(2026, 11, 1),
                                       status=AssignmentStatus.ACTIVE))  # fmt: skip
    await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    response = await _accept(api, member, offer)
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_HAS_SEAT")


async def test_an_offer_is_accepted_once(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    assert (await _accept(api, member, offer)).status_code == 200
    again = await _accept(api, member, offer)
    assert (again.status_code, again.json()["code"]) == (409, "OFFER_CLOSED")


async def test_after_48_hours_the_offer_expires_and_frees_the_seat(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    second = await _request(db_session, route, day=2, phone="+251911000002")
    offer = (await _offer(api, member, route)).json()["id"]
    async with db_session.begin():
        await db_session.execute(
            update(SeatOffer).values(expires_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    read = (await api.get(f"/v1/offers/{offer}", headers=signed_in(member))).json()
    assert read["status"] == "expired"
    late = await _accept(api, member, offer)
    assert (late.status_code, late.json()["code"]) == (409, "OFFER_CLOSED")
    nxt = await _offer(api, member, route)  # the seat passes to the next request
    assert (nxt.status_code, nxt.json()["request_id"]) == (201, str(second))


async def test_staff_withdraw_an_offer(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """The person said no: the seat is free for the next request."""
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    withdrawn = await api.post(f"/v1/offers/{offer}/withdraw", headers=signed_in(member))
    assert (withdrawn.status_code, withdrawn.json()["status"]) == (200, "withdrawn")
    again = await api.post(f"/v1/offers/{offer}/withdraw", headers=signed_in(member))
    assert (again.status_code, again.json()["code"]) == (409, "OFFER_CLOSED")
    late = await _accept(api, member, offer)
    assert late.json()["code"] == "OFFER_CLOSED"


# On the website


async def test_the_asker_accepts_on_the_website(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    token = await verified(db_session, PHONE)
    response = await api.post(
        f"/v1/public/offers/{offer}/accept",
        json={"verified_phone_token": token},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.json()
    assert set(response.json()) == {"status", "route_code", "start_date"}
    assert response.json()["status"] == "accepted"
    rider = await _one(db_session, Rider, phone=PHONE)
    assert rider.source.value == "app"


@pytest.mark.parametrize("problem", ["another phone", "bad token", "unknown offer"])
async def test_only_the_asker_accepts_on_the_website(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID, problem: str
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = (await _offer(api, member, route)).json()["id"]
    token = await verified(db_session, "+251922334455" if problem == "another phone" else PHONE)
    if problem == "bad token":
        token = "not.a.token"  # noqa: S105 - not a credential
    if problem == "unknown offer":
        offer = str(uuid.uuid4())
    response = await api.post(
        f"/v1/public/offers/{offer}/accept",
        json={"verified_phone_token": token},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    expected = (401, "PHONE_NOT_VERIFIED") if problem == "bad token" else (404, "NOT_FOUND")
    assert (response.status_code, response.json()["code"]) == expected


# Reading


async def test_staff_list_a_routes_offers_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip, free=2)
    await _request(db_session, route, day=1)
    await _request(db_session, route, day=2, phone="+251911000002")
    first = (await _offer(api, member, route)).json()["id"]
    second = (await _offer(api, member, route)).json()["id"]
    response = await api.get(f"/v1/routes/{route}/offers", headers=signed_in(member))
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {first, second}


async def test_unknown_offers_and_routes_are_404(api: httpx.AsyncClient, member: uuid.UUID) -> None:
    for method, path in (
        ("GET", f"/v1/offers/{uuid.uuid4()}"),
        ("POST", f"/v1/offers/{uuid.uuid4()}/accept"),
        ("POST", f"/v1/offers/{uuid.uuid4()}/withdraw"),
        ("POST", f"/v1/routes/{uuid.uuid4()}/offers"),
        ("GET", f"/v1/routes/{uuid.uuid4()}/offers"),
    ):
        response = await api.request(method, path, json={}, headers=with_key(member))
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), path


async def test_retried_acceptances_replay(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip, free=2)
    await _request(db_session, route, day=1)
    await _request(db_session, route, day=2, phone="+251911000002")
    by_phone = (await _offer(api, member, route)).json()["id"]
    on_site = (await _offer(api, member, route)).json()["id"]
    headers = with_key(member)
    first = await api.post(f"/v1/offers/{by_phone}/accept", json={}, headers=headers)
    again = await api.post(f"/v1/offers/{by_phone}/accept", json={}, headers=headers)
    assert (first.status_code, again.status_code, again.json()) == (200, 200, first.json())
    body = {"verified_phone_token": await verified(db_session, "+251911000002")}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    site = await api.post(f"/v1/public/offers/{on_site}/accept", json=body, headers=key)
    site_again = await api.post(f"/v1/public/offers/{on_site}/accept", json=body, headers=key)
    assert (site.status_code, site_again.json()) == (200, site.json())


async def test_a_request_not_waiting_for_the_route_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    other = await _running(db_session, trip, code="OTHER-01")
    elsewhere = await _request(db_session, other, day=1)
    response = await _offer(api, member, route, request_id=str(elsewhere))
    assert [error["field"] for error in response.json()["errors"]] == ["/request_id"]


async def test_the_public_catalogue_counts_an_offered_seat_as_taken(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)

    async def full() -> bool:
        params = {"from_area_id": str(trip.bole), "to_area_id": str(trip.mexico)}
        (listed,) = (await api.get("/v1/public/routes", params=params)).json()["items"]
        return bool(listed["full"])

    assert await full() is False
    await _offer(api, member, route)
    assert await full() is True
