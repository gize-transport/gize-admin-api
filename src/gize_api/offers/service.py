"""Seat offer rules (GZ-040). Staff offer, accept (for a caller) and withdraw; the asker may
accept on the website with their verified phone. Staff changes are audited (skip reasons
redacted); website acceptances aren't (the audit log records staff), the rows say so.

- **Offering** locks the route, stores open offers past their time as `expired`, checks a
  seat is free from the start (riders and open offers counted), then locks the queue: the
  oldest adult request waiting for the route with no open offer, or the one staff name
  (ahead of older ones: a skip reason). The offer holds the seat for 48 hours. The SMS
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
from datetime import date, timedelta

from fastapi.responses import JSONResponse, Response
from sqlalchemy import Row, Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import audit, schedule
from gize_api.core.db import borrowed, violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.sms import SmsSender
from gize_api.offers.models import OFFER_OPEN_UNIQUE, SEAT_OFFER_AUDIT, OfferStatus, SeatOffer
from gize_api.offers.schemas import (
    OfferCreate,
    OfferListParams,
    OfferPage,
    OfferRead,
    PublicOfferAccepted,
)
from gize_api.riders import repository as riders
from gize_api.riders.models import RIDER_AUDIT, Rider, RiderPlan, RiderSource, RiderType
from gize_api.route_requests import repository as route_requests
from gize_api.route_requests.models import ROUTE_REQUEST_AUDIT, RequestStatus, RouteRequest
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
    await send_once(
        borrowed(session),
        sender,
        key=f"offer:{offer.id}",
        purpose=SmsPurpose.SEAT_OFFER,
        to=phone,
        text=text,
        telemetry=telemetry,
    )
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
) -> Response:
    """200 with what the asker may see, or the stored response for a retry."""
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
    await session.execute(
        select(RouteRequest.id).where(RouteRequest.id == request.id).with_for_update()
    )
    today = schedule.addis_today()
    if rider is not None and await riders.held_seats(session, [rider.id], today):
        raise AppError(ErrorCode.RIDER_HAS_SEAT)
    if rider is None:
        rider = await _new_rider(session, route, request, caller=caller)
    status = AssignmentStatus.RESERVED
    if route.status is not RouteStatus.FORMING:
        status = AssignmentStatus.ACTIVE
    seat = await seats.take(
        session, route, rider.id, start=offer.start_date, status=status, offer_id=offer.id
    )
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
    """Their rider record, locked (riders before routes): linked to the request, or the
    live adult with its phone."""
    rider_id = request.rider_id
    if rider_id is None:
        rider_id = await session.scalar(
            select(Rider.id).where(
                Rider.phone == request.contact_phone,
                Rider.rider_type == RiderType.ADULT,
                Rider.not_deleted(),
            )
        )
    if rider_id is None:
        return None
    return await riders.get_live_rider(session, rider_id, for_update=True)


async def _new_rider(
    session: AsyncSession, route: Route, request: RouteRequest, *, caller: CurrentStaff | None
) -> Rider:
    """The asker as a rider: the request's name and phone, A to B, round trip."""
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
    await session.flush()
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
    waiting = await route_requests.waiting_without_offer(session, route_id)
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
    """Open offers past their time are expired (under the route's lock)."""
    await session.execute(
        update(SeatOffer)
        .where(
            SeatOffer.route_id == route_id,
            SeatOffer.status == OfferStatus.OPEN,
            SeatOffer.expires_at <= func.now(),
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
