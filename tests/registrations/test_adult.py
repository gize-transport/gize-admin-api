"""GZ-032: an adult signs themselves up from the public app.

`POST /v1/registrations/adult` takes the verified-phone token from GZ-031 (spent once), an
Idempotency-Key, and the rider's name, home and destination areas, plan and Saturday
add-on, with an optional route:
- a `forming` public route with a free seat: a reservation from the next 1st (the route
  locked while seats are counted; `ROUTE_FULL` when none is left);
- an `active` or `paused` one: a route request for it (the public app never gives a place
  on a running route: freed seats go to the oldest request, GZ-040);
- none: a route request for the two areas (`new_areas`).
One live adult rider per phone. Limits: 5/hour per caller, 3/day per phone, failing closed.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.core import schedule
from gize_api.core.db import get_session
from gize_api.core.ratelimit import Policy, RateLimiter
from gize_api.main import GizeAPI
from gize_api.registrations import service
from gize_api.riders.models import Rider, RiderPlan, RiderSource, RiderType
from gize_api.route_requests.models import RequestKind, RequestSource, RouteRequest
from gize_api.route_requests.models import RiderType as RequestRiderType
from gize_api.routes.models import AssignmentStatus, RouteAssignment, RouteStatus, RouteType
from gize_api.verification.models import PhoneVerification
from gize_api.verification.tokens import issue_verified_phone_token, read_verified_phone_token
from tests.factories import CompanyFactory, RouteRequestFactory
from tests.registrations.support import (
    ADULT,
    PHONE,
    Trip,
    add_route,
    add_trip,
    body,
    key,
    take_seats,
    verified,
)
from tests.staff.support import add_admin, signed_in
from tests.verification.support import TOKEN_KEY, broken_redis

OTHER_PHONE = "+251922334455"


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


async def _all[M](session: AsyncSession, model: type[M]) -> list[M]:
    async with session.begin():
        return list((await session.scalars(select(model))).all())


async def _nothing_made(session: AsyncSession) -> None:
    assert await _all(session, Rider) == []
    assert await _all(session, RouteAssignment) == []
    assert await _all(session, RouteRequest) == []


# Reserving a seat on a forming route


async def test_a_forming_route_with_a_free_seat_gives_a_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    route = await add_route(db_session, trip, price_saturday=600)
    token = await verified(db_session)
    response = await api.post(
        ADULT,
        json=body(token, trip, route_id=str(route), plan="morning", saturday=True),
        headers=key(),
    )
    assert response.status_code == 201, response.json()
    assert response.headers["Cache-Control"] == "no-store"
    (rider,) = await _all(db_session, Rider)
    assert response.json() == {
        "rider_id": str(rider.id),
        "outcome": "reserved",
        "route_id": str(route),
        "start_date": "2026-12-01",  # the next 1st: individual riders start on a 1st
        "route_request_id": None,
    }
    assert (rider.rider_type, rider.full_name, rider.phone) == (
        RiderType.ADULT,
        "Abebe Kebede",
        PHONE,
    )
    assert (rider.home_area_id, rider.destination_area_id) == (trip.bole, trip.mexico)
    assert (rider.plan, rider.saturday_addon, rider.source) == (
        RiderPlan.MORNING,
        True,
        RiderSource.APP,
    )
    (seat,) = await _all(db_session, RouteAssignment)
    assert (seat.route_id, seat.rider_id, seat.status) == (
        route,
        rider.id,
        AssignmentStatus.RESERVED,
    )
    assert (seat.start_date, seat.end_date) == (date(2026, 12, 1), None)
    assert await _all(db_session, RouteRequest) == []


async def test_on_the_1st_a_reservation_starts_that_day(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule, "addis_today", lambda: date(2026, 12, 1))
    route = await add_route(db_session, trip)
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, route_id=str(route)), headers=key()
    )
    assert response.json()["start_date"] == "2026-12-01"


async def test_riders_may_board_at_an_area_the_route_serves(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    route = await add_route(db_session, trip, serves=(trip.megenagna,))
    response = await api.post(
        ADULT,
        json=body(
            await verified(db_session), trip, route_id=str(route), home_area_id=str(trip.megenagna)
        ),
        headers=key(),
    )
    assert (response.status_code, response.json()["outcome"]) == (201, "reserved")


async def test_a_full_forming_route_is_409_and_the_token_still_works(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """Nothing is kept: the rider picks another route, or asks for one, with the same
    token."""
    full = await add_route(db_session, trip, seats=2, launch_minimum=2)
    await take_seats(db_session, full, 2)
    token = await verified(db_session)
    response = await api.post(ADULT, json=body(token, trip, route_id=str(full)), headers=key())
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_FULL")
    assert await _all(db_session, Rider) == []
    assert len(await _all(db_session, RouteAssignment)) == 2
    again = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (again.status_code, again.json()["outcome"]) == (201, "requested")


async def test_a_seat_free_only_after_riders_leave_still_counts_them(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """Seats are counted on every day from the start: one rider leaving in March doesn't
    free December."""
    route = await add_route(db_session, trip, seats=1, launch_minimum=1)
    async with db_session.begin():
        db_session.add(
            RouteAssignment(
                route_id=route,
                start_date=date(2026, 12, 1),
                end_date=date(2027, 3, 31),
                status=AssignmentStatus.RESERVED,
            )
        )
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, route_id=str(route)), headers=key()
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_FULL")


# Asking for a seat or a route


@pytest.mark.parametrize("status", [RouteStatus.ACTIVE, RouteStatus.PAUSED])
async def test_a_running_route_gives_a_request_never_a_place(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, status: RouteStatus
) -> None:
    """Even with free seats: a freed seat goes to the oldest request (GZ-040)."""
    route = await add_route(db_session, trip, status=status)
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, route_id=str(route)), headers=key()
    )
    assert response.status_code == 201, response.json()
    (rider,) = await _all(db_session, Rider)
    (request,) = await _all(db_session, RouteRequest)
    assert response.json() == {
        "rider_id": str(rider.id),
        "outcome": "requested",
        "route_id": str(route),
        "start_date": None,
        "route_request_id": str(request.id),
    }
    assert (request.kind, request.route_id, request.from_area_id, request.to_area_id) == (
        RequestKind.ROUTE,
        route,
        None,
        None,
    )
    assert (request.rider_type, request.rider_name, request.rider_id) == (
        RequestRiderType.ADULT,
        "Abebe Kebede",
        rider.id,
    )
    assert (request.contact_name, request.contact_phone, request.source) == (
        "Abebe Kebede",
        PHONE,
        RequestSource.APP,
    )
    assert await _all(db_session, RouteAssignment) == []


async def test_without_a_route_the_two_areas_are_asked_for(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    response = await api.post(
        ADULT,
        json=body(await verified(db_session), trip, destination_area_id=str(trip.summit)),
        headers=key(),
    )
    assert response.status_code == 201, response.json()
    (request,) = await _all(db_session, RouteRequest)
    assert response.json()["outcome"] == "requested"
    assert response.json()["route_id"] is None
    assert response.json()["route_request_id"] == str(request.id)
    assert (request.kind, request.from_area_id, request.to_area_id, request.route_id) == (
        RequestKind.NEW_AREAS,
        trip.bole,
        trip.summit,
        None,
    )


async def test_a_request_staff_recorded_is_linked_to_the_rider(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """The same person asked by phone first: their request keeps its place in the queue."""
    async with db_session.begin():
        recorded = await RouteRequestFactory().create(
            db_session,
            from_area_id=trip.bole,
            from_area_name=None,
            to_area_id=trip.summit,
            to_area_name=None,
            rider_name="Abebe Kebede",
            contact_phone=PHONE,
        )
    response = await api.post(
        ADULT,
        json=body(await verified(db_session), trip, destination_area_id=str(trip.summit)),
        headers=key(),
    )
    assert response.status_code == 201, response.json()
    assert response.json()["route_request_id"] == str(recorded.id)
    (request,) = await _all(db_session, RouteRequest)
    assert (request.rider_id, request.source) == (
        uuid.UUID(response.json()["rider_id"]),
        RequestSource.STAFF,
    )


@pytest.mark.parametrize("status", [RouteStatus.FORMING, RouteStatus.ACTIVE])
async def test_without_a_route_while_one_would_take_them_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, status: RouteStatus
) -> None:
    """A forming route with a seat, or a running one to ask for: they choose it, so the
    demand for these areas isn't counted as a route nobody runs."""
    await add_route(db_session, trip, status=status)
    response = await api.post(ADULT, json=body(await verified(db_session), trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/route_id"]
    await _nothing_made(db_session)


# One rider per phone; the token


async def test_one_live_adult_rider_per_phone(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    first = await api.post(ADULT, json=body(await verified(db_session), trip), headers=key())
    assert first.status_code == 201
    again = await api.post(
        ADULT,
        json=body(await verified(db_session), trip, full_name="Someone Else"),
        headers=key(),
    )
    assert (again.status_code, again.json()["code"]) == (409, "RIDER_EXISTS")
    assert len(await _all(db_session, Rider)) == 1


async def test_a_deleted_rider_frees_the_phone(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    async with db_session.begin():
        db_session.add(
            Rider(
                rider_type=RiderType.ADULT,
                full_name="Old Me",
                phone=PHONE,
                home_area_id=trip.bole,
                destination_area_id=trip.mexico,
                plan=RiderPlan.ROUND_TRIP,
                source=RiderSource.STAFF,
                deleted_at=datetime.now(UTC),
            )
        )
    response = await api.post(ADULT, json=body(await verified(db_session), trip), headers=key())
    assert response.status_code == 201


async def test_the_token_is_spent_by_the_sign_up(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    token = await verified(db_session)
    assert (await api.post(ADULT, json=body(token, trip), headers=key())).status_code == 201
    (verification,) = await _all(db_session, PhoneVerification)
    assert verification.consumed_at is not None
    again = await api.post(ADULT, json=body(token, trip), headers=key())  # a new key
    assert (again.status_code, again.json()["code"]) == (401, "PHONE_NOT_VERIFIED")


async def test_retries_replay_uncounted_even_after_the_token_expires(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost answer retried later (the app came back online) gets the answer, without
    using up the phone's three sign-ups a day (the caller's five an hour still count it)."""
    request, headers = body(await verified(db_session), trip), key()
    first = await api.post(ADULT, json=request, headers=headers)
    assert first.status_code == 201
    monkeypatch.setattr(service, "_now", lambda: datetime(2030, 1, 1, tzinfo=UTC))
    for _ in range(3):  # with the first, four: one more than the phone's three
        again = await api.post(ADULT, json=request, headers=headers)
        assert (again.status_code, again.json()) == (201, first.json())
    fresh = await api.post(ADULT, json=request, headers=key())  # not a retry: expired
    assert (fresh.status_code, fresh.json()["code"]) == (401, "PHONE_NOT_VERIFIED")


async def test_a_retry_with_the_key_replays_the_answer(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """The token is spent by then: the replay comes before it is checked."""
    route = await add_route(db_session, trip)
    request, headers = body(await verified(db_session), trip, route_id=str(route)), key()
    first = await api.post(ADULT, json=request, headers=headers)
    again = await api.post(ADULT, json=request, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert len(await _all(db_session, Rider)) == 1
    assert len(await _all(db_session, RouteAssignment)) == 1


async def test_an_idempotency_key_is_required(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    response = await api.post(ADULT, json=body(await verified(db_session), trip))
    assert (response.status_code, response.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")
    await _nothing_made(db_session)


def _unsigned_for(phone: str) -> str:
    return issue_verified_phone_token(phone, uuid.uuid4(), key="another-key-" + "x" * 32)[0]


@pytest.mark.parametrize(
    "problem", ["garbage", "another key", "expired", "unconfirmed", "another phone", "unknown"]
)
async def test_without_a_good_token_nothing_is_made(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, problem: str
) -> None:
    if problem == "garbage":
        token = "not.a.token"  # noqa: S105 - not a credential
    elif problem == "another key":
        token = _unsigned_for(PHONE)
    elif problem == "expired":
        old = datetime(2026, 1, 1, tzinfo=UTC)
        token = issue_verified_phone_token(PHONE, uuid.uuid4(), key=TOKEN_KEY, now=old)[0]
    elif problem == "unconfirmed":  # signed by us for a code never confirmed
        token = await verified(db_session, confirmed=False)
    elif problem == "another phone":  # the token's phone isn't the one verified
        token = await verified(db_session, token_phone=OTHER_PHONE)
    else:
        token = issue_verified_phone_token(PHONE, uuid.uuid4(), key=TOKEN_KEY)[0]
    response = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (401, "PHONE_NOT_VERIFIED")
    await _nothing_made(db_session)


# Invalid input


async def _corporate(session: AsyncSession, trip: Trip) -> uuid.UUID:
    async with session.begin():
        company = await CompanyFactory().create(session)
    return await add_route(
        session,
        trip,
        route_type=RouteType.CORPORATE,
        company_id=company.id,
        status=RouteStatus.ACTIVE,
        launch_minimum=None,
        price_round_trip=None,
        price_one_way=None,
    )


@pytest.mark.parametrize(
    "problem", ["unknown", "deleted", "draft", "student", "corporate", "elsewhere", "unpriced"]
)
async def test_a_route_the_rider_cannot_take_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, problem: str
) -> None:
    if problem == "unknown":
        route = uuid.uuid4()
    elif problem == "deleted":
        route = await add_route(db_session, trip, deleted_at=datetime.now(UTC))
    elif problem == "draft":
        route = await add_route(db_session, trip, status=RouteStatus.DRAFT)
    elif problem == "student":
        route = await add_route(db_session, trip, route_type=RouteType.STUDENT, school_name="ICS")
    elif problem == "corporate":
        route = await _corporate(db_session, trip)
    elif problem == "elsewhere":  # doesn't go to the rider's destination
        route = await add_route(db_session, trip)
        trip = Trip(trip.bole, trip.megenagna, trip.summit, trip.mexico, trip.corridor)
    else:
        route = await add_route(db_session, trip, price_round_trip=None, price_one_way=None)
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, route_id=str(route)), headers=key()
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/route_id"]
    await _nothing_made(db_session)


@pytest.mark.parametrize("status", [RouteStatus.FORMING, RouteStatus.ACTIVE])
async def test_saturday_needs_a_route_that_offers_it(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, status: RouteStatus
) -> None:
    route = await add_route(db_session, trip, status=status)  # no Saturday price
    response = await api.post(
        ADULT,
        json=body(await verified(db_session), trip, route_id=str(route), saturday=True),
        headers=key(),
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/saturday"]


async def test_saturday_is_kept_as_a_wish_without_a_route(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, saturday=True), headers=key()
    )
    assert response.status_code == 201
    (rider,) = await _all(db_session, Rider)
    assert rider.saturday_addon is True


@pytest.mark.parametrize("field", ["home_area_id", "destination_area_id"])
@pytest.mark.parametrize("problem", ["unknown", "deleted"])
async def test_areas_must_be_live(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, field: str, problem: str
) -> None:
    area = uuid.uuid4()
    if problem == "deleted":
        area = trip.summit
        async with db_session.begin():
            (await db_session.get_one(Area, area)).deleted_at = datetime.now(UTC)
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, **{field: str(area)}), headers=key()
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == [f"/{field}"]


@pytest.mark.parametrize(
    "changes",
    [
        {"full_name": None},
        {"full_name": "   "},
        {"full_name": "x" * 101},
        {"full_name": "Abebe\x00"},
        {"plan": "weekends"},
        {"plan": None},
        {"saturday": "yes"},
        {"home_area_id": "bole"},
        {"route_id": "route-1"},
        {"verified_phone_token": None},
        {"verified_phone_token": "x" * 2001},
        {"phone": PHONE},  # the phone comes from the token, never the body
        {"destination_area_id": "SAME"},
    ],
    ids=str,
)
async def test_a_bad_body_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, changes: dict[str, object]
) -> None:
    if changes.get("destination_area_id") == "SAME":
        changes = {"destination_area_id": str(trip.bole)}
    response = await api.post(
        ADULT, json=body(await verified(db_session), trip, **changes), headers=key()
    )
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    await _nothing_made(db_session)


# Limits, switches, failures, logs


async def test_five_sign_ups_an_hour_per_caller(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    for n in range(5):
        token = await verified(db_session, f"+2519110000{n:02d}")
        assert (await api.post(ADULT, json=body(token, trip), headers=key())).status_code == 201
    token = await verified(db_session, "+251911000099")
    response = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")
    assert "Retry-After" in response.headers


async def test_three_attempts_a_day_per_phone(
    api: httpx.AsyncClient, api_app: GizeAPI, db_session: AsyncSession, trip: Trip
) -> None:
    full = await add_route(db_session, trip, seats=1, launch_minimum=1)
    await take_seats(db_session, full, 1)
    token = await verified(db_session)
    for _ in range(3):
        response = await api.post(ADULT, json=body(token, trip, route_id=str(full)), headers=key())
        assert response.status_code == 409
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as elsewhere:
        response = await elsewhere.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")


async def test_without_redis_nobody_signs_up(
    api: httpx.AsyncClient, api_app: GizeAPI, db_session: AsyncSession, trip: Trip
) -> None:
    """Fails closed, like every public write."""
    token = await verified(db_session)
    async with broken_redis(api_app):
        response = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
    await _nothing_made(db_session)


async def test_without_redis_at_the_phone_check_nobody_signs_up(
    api: httpx.AsyncClient, api_app: GizeAPI, db_session: AsyncSession, trip: Trip
) -> None:
    """The caller is counted, then Redis fails at the phone's count: still closed."""
    token = await verified(db_session)
    working: RateLimiter = api_app.state.rate_limiter

    class PhoneCheckBroken:
        def __init__(self, broken: RateLimiter) -> None:
            self.broken = broken

        async def check(self, policy: Policy, **keys: str | None) -> None:
            await (self.broken if keys.get("phone") else working).check(policy, **keys)

    async with broken_redis(api_app):
        api_app.state.rate_limiter = PhoneCheckBroken(api_app.state.rate_limiter)
        response = await api.post(ADULT, json=body(token, trip), headers=key())
        api_app.state.rate_limiter = api_app.state.rate_limiter.broken
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
    await _nothing_made(db_session)


async def test_switched_off_without_the_token_key(
    api: httpx.AsyncClient, api_app: GizeAPI, db_session: AsyncSession, trip: Trip
) -> None:
    token = await verified(db_session)
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"verified_phone_token_key": None}
    )
    response = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    unreachable_sessions: AsyncSession,
    trip: Trip,
) -> None:
    token = await verified(db_session)

    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.post(ADULT, json=body(token, trip), headers=key())
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


async def test_no_name_or_phone_reaches_the_logs(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    request, headers = body(await verified(db_session), trip), key()
    with structlog.testing.capture_logs() as logs:
        await api.post(ADULT, json=request, headers=headers)
        await api.post(ADULT, json=request, headers=headers)  # a replay logs its scope
    assert any(log["event"] == "idempotent_replay" for log in logs)
    text = repr(logs)
    for secret in ("Abebe", PHONE, PHONE[4:], str(request["verified_phone_token"])):
        assert secret not in text


async def test_the_token_reads_back_as_the_phone_it_proves(db_session: AsyncSession) -> None:
    """The helper issues what GZ-031 issues (guards the tests above)."""
    token = await verified(db_session)
    assert read_verified_phone_token(token, key=TOKEN_KEY).phone == PHONE


async def test_the_areas_a_rider_uses_are_kept(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    """An admin can't delete a live rider's home or destination (AREA_IN_USE)."""
    admin_id = (await add_admin(db_session)).id
    # Two areas no corridor or route uses; only the request and the rider name them.
    request = body(
        await verified(db_session),
        trip,
        home_area_id=str(trip.megenagna),
        destination_area_id=str(trip.summit),
    )
    first = await api.post(ADULT, json=request, headers=key())
    assert first.status_code == 201
    async with db_session.begin():
        await db_session.execute(update(RouteRequest).values(deleted_at=datetime.now(UTC)))
    for area in (trip.megenagna, trip.summit):
        response = await api.delete(f"/v1/areas/{area}", headers=signed_in(admin_id))
        assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")
    async with db_session.begin():
        await db_session.execute(update(Rider).values(deleted_at=datetime.now(UTC)))
    response = await api.delete(f"/v1/areas/{trip.summit}", headers=signed_in(admin_id))
    assert response.status_code == 204
