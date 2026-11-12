"""Seat offer rules (GZ-040). Staff offer, accept (for a caller) and withdraw; the asker may
accept on the website with their verified phone. Staff changes are audited (skip reasons
redacted); website acceptances aren't (the audit log records staff), the rows say so.

- **Offering** locks the route, stores open offers past their time as `expired`, checks a
  seat is free from the start (riders and open offers counted), then locks the queue
  (`GET …/offer-queue` lists it, GZ-075): the oldest adult request waiting for the route
  with no open offer, those offered a seat here before last, or the one staff name (ahead
  of older ones: a skip reason). The offer holds the seat for 48 hours. The SMS
  goes after the commit (GZ-030's `send_once`, `offer:<id>`); its failure keeps the offer.
- **Accepting** locks the rider (when one exists), the route, then the offer and its
  request: an offer no longer open, or past its time, is 409 OFFER_CLOSED. A rider is made
  from the request when the asker has none (A to B, round trip); one already riding
  elsewhere is moved by staff instead (409 RIDER_HAS_SEAT). The seat (`routes.seats.take`,
  counting other open offers): a reservation on a forming route, else a place from the
  offer's 1st. The request is fulfilled.

Lock order (architecture.md): rider, route, offers and requests.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Row, Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import audit, schedule
from gize_api.core.db import borrowed, violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, peek, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.ratelimit import PUBLIC_SIGNUP, RateLimiter
from gize_api.core.security import CurrentStaff
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.sms import SmsSender
from gize_api.offers.models import OFFER_OPEN_UNIQUE, SEAT_OFFER_AUDIT, OfferStatus, SeatOffer
from gize_api.offers.schemas import (
    OfferCreate,
    OfferListParams,
    OfferPage,
    OfferQueueItem,
    OfferQueuePage,
    OfferQueueParams,
    OfferRead,
    PublicOfferAccepted,
    encode_queue_cursor,
)
from gize_api.riders import repository as riders
from gize_api.riders import service as rider_service
from gize_api.riders.models import (
    RIDER_AUDIT,
    Rider,
    RiderPlan,
    RiderSource,
    RiderType,
)
from gize_api.route_requests import repository as route_requests
from gize_api.route_requests.models import ROUTE_REQUEST_AUDIT, RequestStatus, RouteRequest
from gize_api.route_requests.schemas import RouteRequestRead
from gize_api.routes import repository as routes
from gize_api.routes import seats
from gize_api.routes.models import (
    ROUTE_ASSIGNMENT_AUDIT,
    AssignmentStatus,
    Route,
    RouteStatus,
    RouteType,
)
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from gize_api.sms.service import send_once
from gize_api.verification.service import spend
from gize_api.verification.tokens import VerifiedPhone

OFFER_LIFETIME = timedelta(hours=48)

logger = structlog.get_logger("gize_api.offers")


def _now() -> datetime:
    return datetime.now(UTC)


def message(code: str, start: date) -> str:
    return (
        f"Gize: a seat on route {code} is free for you from {start.day} {start:%B %Y}. "
        "Accept within 48 hours on Gize's website or by calling Gize."
    )


@dataclass(frozen=True)
class _Accepted:
    offer: SeatOffer
    route: Route


async def create(
    session: AsyncSession,
    caller: CurrentStaff,
    route_id: uuid.UUID,
    body: OfferCreate,
    idempotent: IdempotentRequest,
    *,
    sender: SmsSender,
    telemetry: Telemetry | None,
) -> Response:
    """201 with a Location, or the stored response for a retry. The SMS follows the
    commit."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        route = await _route(session, route_id)
        if route.route_type is not RouteType.PUBLIC or route.status not in (
            RouteStatus.FORMING,
            RouteStatus.ACTIVE,
        ):
            raise AppError(ErrorCode.ROUTE_NOT_READY)
        await _expire_stale(session, route.id)
        start = _start(route, body.start_date, schedule.addis_today())
        await seats.full_or_raise(session, route, start)
        request, skip_reason = await _whom(session, route.id, body)
        offer = SeatOffer(
            route_id=route.id,
            request_id=request.id,
            start_date=start,
            expires_at=func.now() + OFFER_LIFETIME,
            skip_reason=skip_reason,
        )
        session.add(offer)
        await _flush(session)
        await session.refresh(offer)
        audit.record(
            session,
            actor_id=caller.id,
            action="seat_offer.created",
            entity=SEAT_OFFER_AUDIT,
            entity_id=offer.id,
            before=None,
            after=audit.snapshot(SEAT_OFFER_AUDIT, offer),
        )
        payload = _read(offer, route.code, request.rider_name, None).model_dump(mode="json")
        location = f"/v1/offers/{offer.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
        phone, text = request.contact_phone, message(route.code, start)
    try:
        await send_once(
            borrowed(session),
            sender,
            key=f"offer:{offer.id}",
            purpose=SmsPurpose.SEAT_OFFER,
            to=phone,
            text=text,
            telemetry=telemetry,
        )
    except Exception:
        logger.warning("offer_sms_failed", offer_id=str(offer.id), exc_info=True)
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def get(session: AsyncSession, offer_id: uuid.UUID) -> OfferRead:
    async with session.begin():
        found = (await session.execute(_offers().where(SeatOffer.id == offer_id))).one_or_none()
        if found is None:
            raise AppError(ErrorCode.NOT_FOUND)
        return _from_row(found)


async def list_offers(
    session: AsyncSession, route_id: uuid.UUID, params: OfferListParams
) -> OfferPage:
    async with session.begin():
        if await routes.get_live_route(session, route_id) is None:
            raise AppError(ErrorCode.NOT_FOUND)
        page = await paginate(
            session,
            select(SeatOffer).where(SeatOffer.route_id == route_id),
            params,
            created_at=SeatOffer.created_at,
            id=SeatOffer.id,
            newest_first=True,
        )
        ids = [offer.id for offer in page.items]
        listed = await session.execute(_offers().where(SeatOffer.id.in_(ids)))
        rows = {row[0].id: row for row in listed}
        return OfferPage(
            items=[_from_row(rows[offer_id]) for offer_id in ids], next_cursor=page.next_cursor
        )


async def queue(
    session: AsyncSession, route_id: uuid.UUID, params: OfferQueueParams
) -> OfferQueuePage:
    """The requests waiting for the route in the order the offer picks (GZ-075), whatever
    the route's status: only offering is refused on a draft or paused one."""
    async with session.begin():
        if await routes.get_live_route(session, route_id) is None:
            raise AppError(ErrorCode.NOT_FOUND)
        entries = await route_requests.offer_queue(
            session, route_id, after=params.queue_after, limit=params.limit + 1
        )
        page = entries[: params.limit]
        return OfferQueuePage(
            items=[
                OfferQueueItem(
                    position=entry.position,
                    offered_before=entry.offered_before,
                    not_offerable=entry.not_offerable,
                    request=RouteRequestRead.model_validate(entry.request),
                )
                for entry in page
            ],
            next_cursor=encode_queue_cursor(page[-1].key) if len(entries) > params.limit else None,
        )


async def accept_by_staff(
    session: AsyncSession, caller: CurrentStaff, offer_id: uuid.UUID, idempotent: IdempotentRequest
) -> Response:
    """200 with the offer, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        accepted = await _accept(session, offer_id, caller=caller, proved=None)
        payload = (await _read_one(session, accepted.offer.id)).model_dump(mode="json")
        await record(session, claimed, status=200, body=payload)
    return JSONResponse(payload, status_code=200)


async def accept_on_website(
    session: AsyncSession,
    offer_id: uuid.UUID,
    proved: VerifiedPhone,
    *,
    scope: str,
    idempotent: IdempotentRequest,
    limiter: RateLimiter,
) -> Response:
    """200 with what the asker may see, or the stored response for a retry: replayed
    before the token's expiry and the phone's count are checked, as for a sign-up."""
    async with session.begin():
        stored = await peek(session, scope=scope, request=idempotent)
    if stored is not None:
        return replay_response(stored)
    if proved.expires_at <= _now():
        raise AppError(ErrorCode.PHONE_NOT_VERIFIED)
    await limiter.check(PUBLIC_SIGNUP, phone=proved.phone)
    async with session.begin():
        claimed = await claim(session, scope=scope, request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        await spend(session, proved)
        accepted = await _accept(session, offer_id, caller=None, proved=proved)
        payload = PublicOfferAccepted(
            status=accepted.offer.status,
            route_code=accepted.route.code,
            start_date=accepted.offer.start_date,
        ).model_dump(mode="json")
        await record(session, claimed, status=200, body=payload)
    return JSONResponse(payload, status_code=200)


async def withdraw(session: AsyncSession, caller: CurrentStaff, offer_id: uuid.UUID) -> OfferRead:
    """The person said no, or staff changed their mind: the seat is free again."""
    async with session.begin():
        offer = await session.get(SeatOffer, offer_id)
        if offer is None:
            raise AppError(ErrorCode.NOT_FOUND)
        await _route(session, offer.route_id)
        offer = await _locked_open(session, offer_id)
        before = audit.snapshot(SEAT_OFFER_AUDIT, offer)
        offer.status = OfferStatus.WITHDRAWN
        await session.flush()
        audit.record(
            session,
            actor_id=caller.id,
            action="seat_offer.withdrawn",
            entity=SEAT_OFFER_AUDIT,
            entity_id=offer.id,
            before=before,
            after=audit.snapshot(SEAT_OFFER_AUDIT, offer),
        )
        return await _read_one(session, offer.id)


async def _accept(
    session: AsyncSession,
    offer_id: uuid.UUID,
    *,
    caller: CurrentStaff | None,
    proved: VerifiedPhone | None,
) -> _Accepted:
    found = (
        await session.execute(
            select(SeatOffer, RouteRequest)
            .join(RouteRequest, RouteRequest.id == SeatOffer.request_id)
            .where(SeatOffer.id == offer_id)
        )
    ).one_or_none()
    if found is None or (proved is not None and found[1].contact_phone != proved.phone):
        raise AppError(ErrorCode.NOT_FOUND)  # someone else's offer: the same answer
    offer, request = found
    rider = await _askers_rider(session, request)
    route = await _route(session, offer.route_id)  # live: its open request keeps it so
    offer = await _locked_open(session, offer_id)
    request = await _still_waiting(session, request.id, route.id)
    if route.status not in (RouteStatus.FORMING, RouteStatus.ACTIVE):
        raise AppError(ErrorCode.OFFER_CLOSED)  # back to draft, or paused, meanwhile
    today = schedule.addis_today()
    if rider is not None and await riders.held_seats(session, [rider.id], today):
        raise AppError(ErrorCode.RIDER_HAS_SEAT)
    if rider is None:
        rider = await _new_rider(session, route, request, caller=caller)
    status, start = AssignmentStatus.RESERVED, offer.start_date
    if route.status is RouteStatus.ACTIVE:
        # Launched meanwhile, perhaps for a later month: riding starts when it runs.
        launched = await routes.latest_launch_day(session, route.id)
        status = AssignmentStatus.ACTIVE
        start = max(start, routes.next_launch_day(today), launched or start)
    seat = await seats.take(session, route, rider.id, start=start, status=status, offer_id=offer.id)
    offer_before = audit.snapshot(SEAT_OFFER_AUDIT, offer)
    request_before = audit.snapshot(ROUTE_REQUEST_AUDIT, request)
    offer.status, offer.rider_id = OfferStatus.ACCEPTED, rider.id
    request.status, request.rider_id = RequestStatus.FULFILLED, rider.id
    await session.flush()
    if caller is not None:
        for action, entity, entity_id, before, row in (
            ("route_assignment.created", ROUTE_ASSIGNMENT_AUDIT, seat.id, None, seat),
            ("seat_offer.accepted", SEAT_OFFER_AUDIT, offer.id, offer_before, offer),
            ("route_request.status_changed", ROUTE_REQUEST_AUDIT, request.id, request_before,
             request),
        ):  # fmt: skip
            audit.record(
                session,
                actor_id=caller.id,
                action=action,
                entity=entity,
                entity_id=entity_id,
                before=before,
                after=audit.snapshot(entity, row),
            )
    return _Accepted(offer, route)


async def _askers_rider(session: AsyncSession, request: RouteRequest) -> Rider | None:
    """Their rider record, locked (riders before routes): the one linked to the request,
    else the live adult with its phone and name (ignoring case: someone else asking with
    a relative's phone isn't that relative, GZ-035's rule)."""
    if request.rider_id is not None:
        linked = await riders.get_live_rider(session, request.rider_id, for_update=True)
        if linked is not None:
            return linked
    rider_id = await session.scalar(
        select(Rider.id).where(
            Rider.phone == request.contact_phone,
            func.lower(Rider.full_name) == func.lower(request.rider_name),
            Rider.rider_type == RiderType.ADULT,
            Rider.not_deleted(),
        )
    )
    if rider_id is None:
        return None
    return await riders.get_live_rider(session, rider_id, for_update=True)


async def _still_waiting(
    session: AsyncSession, request_id: uuid.UUID, route_id: uuid.UUID
) -> RouteRequest:
    """The request, locked, still live, open and waiting for this route (staff may have
    declined, fulfilled or re-matched it meanwhile); 409 OFFER_CLOSED otherwise."""
    request = await session.scalar(
        select(RouteRequest)
        .where(RouteRequest.id == request_id, *route_requests.waiting_for(route_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise AppError(ErrorCode.OFFER_CLOSED)
    return request


async def _new_rider(
    session: AsyncSession, route: Route, request: RouteRequest, *, caller: CurrentStaff | None
) -> Rider:
    """The asker as a rider: the request's name and phone, A to B, round trip. 409
    RIDER_EXISTS when another live adult has the phone (staff sort out who is who)."""
    if await riders.phone_taken(session, request.contact_phone, company_id=None, except_id=None):
        raise AppError(ErrorCode.RIDER_EXISTS)
    rider = Rider(
        rider_type=RiderType.ADULT,
        full_name=request.rider_name,
        phone=request.contact_phone,
        home_area_id=route.corridor.origin_area_id,
        destination_area_id=route.corridor.destination_area_id,
        plan=RiderPlan.ROUND_TRIP,
        source=RiderSource.STAFF if caller is not None else RiderSource.APP,
    )
    session.add(rider)
    await rider_service.flush_riders(session)  # the same phone, signed up meanwhile: 409
    await session.refresh(rider)
    if caller is not None:
        audit.record(
            session,
            actor_id=caller.id,
            action="rider.created",
            entity=RIDER_AUDIT,
            entity_id=rider.id,
            before=None,
            after=audit.snapshot(RIDER_AUDIT, rider),
        )
    return rider


async def _whom(
    session: AsyncSession, route_id: uuid.UUID, body: OfferCreate
) -> tuple[RouteRequest, str | None]:
    """The request to offer to, and the skip reason when older ones were passed over."""
    waiting = await route_requests.waiting_without_offer(session, route_id, also=body.request_id)
    if body.request_id is None:
        if not waiting:
            raise AppError(ErrorCode.NO_WAITING_REQUESTS)
        return waiting[0], None
    position = next(
        (index for index, request in enumerate(waiting) if request.id == body.request_id), None
    )
    if position is None:
        if await _has_open_offer(session, body.request_id):
            raise AppError(ErrorCode.OFFER_OPEN)
        raise invalid_field("body", "request_id")  # not waiting for this route
    if position > 0 and body.skip_reason is None:
        raise AppError(ErrorCode.ROUTE_HAS_WAITING_REQUESTS)
    return waiting[position], body.skip_reason if position > 0 else None


async def _has_open_offer(session: AsyncSession, request_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(SeatOffer.id).where(
                SeatOffer.request_id == request_id,
                SeatOffer.status == OfferStatus.OPEN,
                SeatOffer.expires_at > func.now(),
            )
        )
    ) is not None


def _start(route: Route, requested: date | None, today: date) -> date:
    """A forming route: the next 1st (a reservation; the launch moves it). An active one:
    a coming 1st, the next by default, at most a year ahead (422 on `start_date`)."""
    earliest = routes.next_launch_day(today)
    if route.status is RouteStatus.FORMING:
        return earliest
    start = requested or earliest
    if start.day != 1 or not earliest <= start <= date(today.year + 1, today.month, 1):
        raise invalid_field("body", "start_date")
    return start


async def _route(session: AsyncSession, route_id: uuid.UUID) -> Route:
    route = await routes.get_live_route(session, route_id, for_update=True)
    if route is None:
        raise AppError(ErrorCode.NOT_FOUND)
    return route


async def _expire_stale(session: AsyncSession, route_id: uuid.UUID) -> None:
    """Open offers past their time are stored expired (under the route's lock): this
    route's, and those of requests waiting for it (an offer on another route that lapsed
    would otherwise still count as the request's one open offer)."""
    waiting = select(RouteRequest.id).where(*route_requests.waiting_for(route_id))
    await session.execute(
        update(SeatOffer)
        .where(
            SeatOffer.status == OfferStatus.OPEN,
            SeatOffer.expires_at <= func.now(),
            or_(SeatOffer.route_id == route_id, SeatOffer.request_id.in_(waiting)),
        )
        .values(status=OfferStatus.EXPIRED)
    )


async def _locked_open(session: AsyncSession, offer_id: uuid.UUID) -> SeatOffer:
    """The offer, locked, still open and in time; 409 OFFER_CLOSED otherwise."""
    found = (
        await session.execute(
            select(SeatOffer, SeatOffer.expires_at > func.now())
            .where(SeatOffer.id == offer_id)
            .with_for_update(of=SeatOffer)
            .execution_options(populate_existing=True)
        )
    ).one()
    offer, in_time = found
    if offer.status is not OfferStatus.OPEN or not in_time:
        raise AppError(ErrorCode.OFFER_CLOSED)
    return offer


def _offers() -> Select[SeatOffer, str, str, SmsStatus, bool]:
    """Offers with their route's code, asker's name, SMS status and whether still in
    time."""
    return (
        select(
            SeatOffer,
            Route.code,
            RouteRequest.rider_name,
            SmsMessage.status,
            SeatOffer.expires_at > func.now(),
        )
        .join(Route, Route.id == SeatOffer.route_id)
        .join(RouteRequest, RouteRequest.id == SeatOffer.request_id)
        .outerjoin(
            SmsMessage,
            SmsMessage.dedup_key == func.concat("offer:", SeatOffer.id),
        )
    )


async def _read_one(session: AsyncSession, offer_id: uuid.UUID) -> OfferRead:
    return _from_row((await session.execute(_offers().where(SeatOffer.id == offer_id))).one())


def _from_row(row: Row[SeatOffer, str, str, SmsStatus, bool]) -> OfferRead:
    return _read(row[0], row[1], row[2], row[3], row[4])


def _read(
    offer: SeatOffer,
    code: str,
    rider_name: str,
    sms_status: SmsStatus | None,
    in_time: bool | None = None,
) -> OfferRead:
    status = offer.status
    if status is OfferStatus.OPEN and in_time is False:
        status = OfferStatus.EXPIRED  # nothing runs on a timer: expired when read
    return OfferRead(
        id=offer.id,
        route_id=offer.route_id,
        route_code=code,
        request_id=offer.request_id,
        rider_name=rider_name,
        start_date=offer.start_date,
        status=status,
        expires_at=offer.expires_at,
        skip_reason=offer.skip_reason,
        rider_id=offer.rider_id,
        sms_status=sms_status,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )


async def _flush(session: AsyncSession) -> None:
    """Flush; another offer to the same request made meanwhile is the same 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if violated_unique_constraint(exc) == OFFER_OPEN_UNIQUE:
            raise AppError(ErrorCode.OFFER_OPEN) from None
        raise
