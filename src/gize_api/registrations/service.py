"""Public sign-up (GZ-032): an adult registers from the rider app.

1. The verified-phone token (GZ-031) is read: the phone comes from it, never the body.
2. The per-phone limit (`PUBLIC_SIGNUP`; the route counts the caller).
3. One transaction: claim the Idempotency-Key (scoped by a keyed hash of the phone; a
   retry replays the answer, even though the token is spent by then), spend the token
   (its verification locked: one sign-up per token), lock the chosen route, share-lock
   the areas, then create the rider and either a reservation (a forming route with a free
   seat) or a route request (a running route, or no route: the two areas), and record
   the answer. Any refusal rolls everything back, the token included: the rider can try
   another route with it.

Locks: the route first (the seat count, `routes.seats`), then the areas, as staff edits
of a route's areas do. Public sign-ups aren't in the audit log (it records staff changes);
the rows say `source = app`.
"""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.repository import get_live_area
from gize_api.core import schedule
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.ratelimit import PUBLIC_SIGNUP, RateLimiter
from gize_api.registrations.schemas import (
    AdultRegistered,
    AdultRegistration,
    RegistrationOutcome,
)
from gize_api.riders.models import (
    RIDER_ADULT_PHONE_UNIQUE,
    Rider,
    RiderSource,
    RiderType,
)
from gize_api.route_requests import service as route_requests
from gize_api.routes import repository as routes
from gize_api.routes import seats
from gize_api.routes.models import Route, RouteStatus, RouteType
from gize_api.verification.models import PhoneVerification
from gize_api.verification.tokens import (
    InvalidVerifiedPhoneTokenError,
    VerifiedPhone,
    phone_scope,
    read_verified_phone_token,
)

_OPEN_TO_RIDERS = (RouteStatus.FORMING, RouteStatus.ACTIVE, RouteStatus.PAUSED)


async def register_adult(
    session: AsyncSession,
    registration: AdultRegistration,
    *,
    idempotent: IdempotentRequest,
    key: str,
    limiter: RateLimiter,
) -> Response:
    """201 with what the sign-up got, or the stored answer for a retry."""
    proved = _proved(registration.verified_phone_token, key)
    await limiter.check(PUBLIC_SIGNUP, phone=proved.phone)
    async with session.begin():
        claimed = await claim(session, scope=phone_scope(proved.phone, key=key), request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        await _spend(session, proved)
        route = await _route_to_take(session, registration)
        for field in ("home_area_id", "destination_area_id"):
            if await get_live_area(session, getattr(registration, field), lock="share") is None:
                raise invalid_field("body", field)
        if route is not None:
            await _fits_the_trip(session, route, registration)
        rider = await _new_rider(session, registration, proved.phone)
        registered = await _seat_or_request(session, rider, route, registration, proved.phone)
        payload = registered.model_dump(mode="json")
        await record(session, claimed, status=201, body=payload)
    return JSONResponse(payload, status_code=201)


def _proved(token: str, key: str) -> VerifiedPhone:
    try:
        return read_verified_phone_token(token, key=key)
    except InvalidVerifiedPhoneTokenError:
        raise AppError(ErrorCode.PHONE_NOT_VERIFIED) from None


async def _spend(session: AsyncSession, proved: VerifiedPhone) -> None:
    """The token's verification, confirmed for this phone and not spent yet, is spent now
    (locked: two sign-ups with one token, one gets through)."""
    verification = await session.scalar(
        select(PhoneVerification)
        .where(PhoneVerification.id == proved.verification_id)
        .with_for_update()
    )
    if (
        verification is None
        or verification.phone != proved.phone
        or verification.verified_at is None
        or verification.consumed_at is not None
    ):
        raise AppError(ErrorCode.PHONE_NOT_VERIFIED)
    verification.consumed_at = func.now()


async def _route_to_take(session: AsyncSession, registration: AdultRegistration) -> Route | None:
    """The chosen route, locked: a live public route taking riders, with prices."""
    if registration.route_id is None:
        return None
    route = await routes.get_live_route(session, registration.route_id, for_update=True)
    if (
        route is None
        or route.route_type is not RouteType.PUBLIC
        or route.status not in _OPEN_TO_RIDERS
        or route.price_round_trip is None
        or route.price_one_way is None
    ):
        raise invalid_field("body", "route_id")
    return route


async def _fits_the_trip(
    session: AsyncSession, route: Route, registration: AdultRegistration
) -> None:
    home, destination = registration.home_area_id, registration.destination_area_id
    if not await routes.serves(session, route.id, home, destination):
        raise invalid_field("body", "route_id")
    if registration.saturday and route.price_saturday is None:
        raise invalid_field("body", "saturday")


async def _new_rider(session: AsyncSession, registration: AdultRegistration, phone: str) -> Rider:
    taken = exists().where(
        Rider.phone == phone, Rider.rider_type == RiderType.ADULT, Rider.not_deleted()
    )
    if await session.scalar(select(taken)):
        raise AppError(ErrorCode.RIDER_EXISTS)
    rider = Rider(
        id=uuid.uuid4(),
        rider_type=RiderType.ADULT,
        full_name=registration.full_name,
        phone=phone,
        home_area_id=registration.home_area_id,
        destination_area_id=registration.destination_area_id,
        plan=registration.plan,
        saturday_addon=registration.saturday,
        source=RiderSource.APP,
    )
    session.add(rider)
    try:
        await session.flush()
    except IntegrityError as exc:  # the same phone, signed up meanwhile
        _rider_exists_or_raise(exc)
    return rider


async def _seat_or_request(
    session: AsyncSession,
    rider: Rider,
    route: Route | None,
    registration: AdultRegistration,
    phone: str,
) -> AdultRegistered:
    """A seat on a forming route; a request for a running one, or for the areas. The
    public app never gives a place on a running route: freed seats go to the oldest
    request (GZ-040)."""
    if route is not None and route.status is RouteStatus.FORMING:
        seat = await seats.reserve(session, route, rider.id, schedule.addis_today())
        return AdultRegistered(
            rider_id=rider.id,
            outcome=RegistrationOutcome.RESERVED,
            route_id=route.id,
            start_date=seat.start_date,
            route_request_id=None,
        )
    request = await route_requests.add_from_app(
        session,
        rider_id=rider.id,
        rider_name=registration.full_name,
        phone=phone,
        route=route,
        from_area_id=registration.home_area_id,
        to_area_id=registration.destination_area_id,
    )
    return AdultRegistered(
        rider_id=rider.id,
        outcome=RegistrationOutcome.REQUESTED,
        route_id=None if route is None else route.id,
        start_date=None,
        route_request_id=request.id,
    )


def _rider_exists_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == RIDER_ADULT_PHONE_UNIQUE:
        raise AppError(ErrorCode.RIDER_EXISTS) from None
    raise exc
