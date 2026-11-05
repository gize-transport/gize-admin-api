"""Rider business rules (GZ-034, GZ-035). Any active staff member records, lists, looks up,
reads and edits riders, and gives, moves and ends their seats; only admins delete riders
(soft), and never while they hold a seat. Every staff change is audited, with names, phones
and skip reasons redacted.

- Adults ride `public` routes, from a 1st (individual riders prepay whole months): a
  reservation on a `forming` route, a place on an `active` one; they leave at a month's
  end. Placing one on an active route ahead of other people's open requests for it needs
  a skip reason; their own open requests for it are fulfilled.
- Company riders ride their company's `corporate` route, starting and leaving on any day.
- A rider holding a seat must still fit its route after an edit: boarding at A or an area
  it serves, going to B (an adult's destination), Saturday only where offered.
- A move ends the old seat the day before the new one starts; a seat that hasn't started
  by then (a reservation, or a move still to come) just ends: it never rode. Placing a
  rider back on the route of a seat they are leaving undoes the end. A rider who is
  riding isn't moved to a forming route: it may launch late or never.

Locks (architecture.md, "Row locks"): the rider, then the routes of their seats and the
new one in a fixed order, then the route's requests. Seats are counted under the route's
lock (`routes.seats.take`).
"""

import uuid
from datetime import date, timedelta
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.repository import get_live_area
from gize_api.companies.repository import get_live_company
from gize_api.core import audit, schedule
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.corridors.schemas import AreaRef
from gize_api.riders import repository
from gize_api.riders.models import (
    RIDER_ADULT_PHONE_UNIQUE,
    RIDER_AUDIT,
    RIDER_COMPANY_PHONE_UNIQUE,
    Rider,
    RiderSource,
    RiderType,
)
from gize_api.riders.repository import HeldSeat
from gize_api.riders.schemas import (
    MAX_LOOKUP,
    RiderCreate,
    RiderListItem,
    RiderListParams,
    RiderLookup,
    RiderMatches,
    RiderPage,
    RiderRead,
    RiderUpdate,
    SeatEnd,
    SeatPlacement,
    SeatRef,
    SeatState,
    masked,
)
from gize_api.route_requests import repository as route_requests
from gize_api.route_requests.models import ROUTE_REQUEST_AUDIT, RequestStatus, RouteRequest
from gize_api.routes import repository as routes
from gize_api.routes import seats
from gize_api.routes.models import (
    ROUTE_ASSIGNMENT_AUDIT,
    AssignmentStatus,
    Route,
    RouteAssignment,
    RouteStatus,
    RouteType,
)
from gize_api.routes.schemas import CompanyRef

_AREA_FIELDS = ("home_area_id", "destination_area_id")
_ADULTS_ONLY = ("destination_area_id", "plan", "saturday_addon")
_A_YEAR = timedelta(days=365)


async def create(
    session: AsyncSession, caller: CurrentStaff, rider: RiderCreate, idempotent: IdempotentRequest
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        if rider.company_id is not None:  # a company rider (RiderCreate checks the pair)
            company = await get_live_company(session, rider.company_id, lock="share")
            if company is None or not company.is_active:
                raise invalid_field("body", "company_id")
        await _refer_to_areas(
            session, {"home_area_id": rider.home_area_id,
                      "destination_area_id": rider.destination_area_id},
        )  # fmt: skip
        if await repository.phone_taken(
            session, rider.phone, company_id=rider.company_id, except_id=None
        ):
            raise AppError(ErrorCode.RIDER_EXISTS)
        row = Rider(
            rider_type=RiderType(rider.rider_type.value),
            full_name=rider.full_name,
            phone=rider.phone,
            company_id=rider.company_id,
            home_area_id=rider.home_area_id,
            destination_area_id=rider.destination_area_id,
            plan=rider.plan,
            saturday_addon=rider.saturday_addon,
            source=RiderSource.STAFF,
        )
        session.add(row)
        await flush_riders(session)
        row = await repository.reload(session, row)
        audit.record(
            session,
            actor_id=caller.id,
            action="rider.created",
            entity=RIDER_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(RIDER_AUDIT, row),
        )
        payload = _read(row, [], 0).model_dump(mode="json")
        location = f"/v1/riders/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_riders(session: AsyncSession, params: RiderListParams) -> RiderPage:
    """Newest first, each with their seats and open requests (four queries per page)."""
    today = schedule.addis_today()
    seat = params.seat
    async with session.begin():
        page = await paginate(
            session,
            repository.live_riders(
                q=params.q,
                rider_type=params.rider_type,
                seat=None if seat in (None, SeatState.NONE) else AssignmentStatus(seat),
                no_seat=seat is SeatState.NONE,
                route_id=params.route_id,
                today=today,
            ),
            params,
            created_at=Rider.created_at,
            id=Rider.id,
            newest_first=True,
        )
        return RiderPage(
            items=await _listed(session, page.items, today), next_cursor=page.next_cursor
        )


async def lookup(session: AsyncSession, lookup: RiderLookup) -> RiderMatches:
    """Live riders with this phone, newest first."""
    today = schedule.addis_today()
    async with session.begin():
        found = await repository.with_phone(session, lookup.phone, limit=MAX_LOOKUP)
        return RiderMatches(items=await _listed(session, found, today))


async def get(session: AsyncSession, rider_id: uuid.UUID) -> RiderRead:
    async with session.begin():
        return await _read_with_seats(session, await _live(session, rider_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, rider_id: uuid.UUID, change: RiderUpdate
) -> RiderRead:
    """Setting what is already there changes and records nothing."""
    async with session.begin():
        row = await _live(session, rider_id, for_update=True)
        if row.rider_type is RiderType.CORPORATE:
            for field in _ADULTS_ONLY:
                if field in change.model_fields_set:
                    raise invalid_field("body", field)
        today = schedule.addis_today()
        _, held_routes = await _lock_seat_routes(session, row.id, today)
        home = change.home_area_id or row.home_area_id
        destination = change.destination_area_id or row.destination_area_id
        moved = [field for field in _AREA_FIELDS if field in change.model_fields_set]
        if moved and home == destination:
            raise invalid_field("body", moved[-1])
        await _refer_to_areas(session, {field: getattr(change, field) for field in moved})
        for route in held_routes:
            if moved:
                await _still_on_the_way(session, route, row, home, destination, moved)
            if change.saturday_addon and route.price_saturday is None:
                raise invalid_field("body", "saturday_addon")
        if change.phone is not None and await repository.phone_taken(
            session, change.phone, company_id=row.company_id, except_id=row.id
        ):
            raise AppError(ErrorCode.RIDER_EXISTS)
        before = audit.snapshot(RIDER_AUDIT, row)
        for field in ("full_name", "phone", *moved, "plan", "saturday_addon"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        await flush_riders(session)
        row = await repository.reload(session, row)
        audit.record(
            session,
            actor_id=caller.id,
            action="rider.updated",
            entity=RIDER_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(RIDER_AUDIT, row),
        )
        return await _read_with_seats(session, row)


async def place(
    session: AsyncSession,
    caller: CurrentStaff,
    rider_id: uuid.UUID,
    placement: SeatPlacement,
    idempotent: IdempotentRequest,
) -> Response:
    """Give the rider a seat on the route, or move them there: 200 with the rider, or the
    stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        row = await _live(session, rider_id, for_update=True)
        today = schedule.addis_today()
        held, locked = await _lock_seat_routes(session, row.id, today, also=placement.route_id)
        route = next((each for each in locked if each.id == placement.route_id), None)
        if route is None or not await _takes(session, route, row):
            raise invalid_field("body", "route_id")
        if row.company_id is not None:
            await _company_takes_riders(session, row.company_id)
        staying = next((seat for seat in held if seat.route_id == route.id), None)
        if staying is not None:
            await _stay(session, caller, route, staying, held, today)
        else:
            await _move(session, caller, route, row, held, placement, today)
        payload = (await _read_with_seats(session, row)).model_dump(mode="json")
        await record(session, claimed, status=200, body=payload)
    return JSONResponse(payload, status_code=200)


async def _move(
    session: AsyncSession,
    caller: CurrentStaff,
    route: Route,
    row: Rider,
    held: list[HeldSeat],
    placement: SeatPlacement,
    today: date,
) -> None:
    """A new seat on `route` (locked); what the rider holds ends the day before it starts,
    and a seat not started yet ends at once: it is replaced. One move at a time: with a
    move planned (two seats), 409 RIDER_MOVING until it is undone (`_stay`)."""
    if len(held) > 1:
        raise AppError(ErrorCode.RIDER_MOVING)
    riding = any(seat.status is AssignmentStatus.ACTIVE for seat in held)
    if riding and route.status is RouteStatus.FORMING:
        # A forming route may launch late or never: they'd be left without a seat.
        raise invalid_field("body", "route_id")
    start = _start(row, placement.start_date, today)
    own: list[uuid.UUID] = []
    skip_reason = None
    if row.rider_type is RiderType.ADULT and route.status is RouteStatus.ACTIVE:
        own, skip_reason = await _queue(session, route, row, placement.skip_reason)
    for seat in held:
        before_it = seat.start_date if seat.start_date > today else start
        await _end_seat(session, caller, seat, last_day=before_it - timedelta(days=1))
    status = AssignmentStatus.RESERVED
    if route.status is not RouteStatus.FORMING:
        status = AssignmentStatus.ACTIVE
    taken = await seats.take(
        session, route, row.id, start=start, status=status, skip_reason=skip_reason
    )
    audit.record(
        session,
        actor_id=caller.id,
        action="route_assignment.created",
        entity=ROUTE_ASSIGNMENT_AUDIT,
        entity_id=taken.id,
        before=None,
        after=audit.snapshot(ROUTE_ASSIGNMENT_AUDIT, taken),
    )
    await _fulfil(session, caller, own)


async def _stay(
    session: AsyncSession,
    caller: CurrentStaff,
    route: Route,
    staying: HeldSeat,
    held: list[HeldSeat],
    today: date,
) -> None:
    """Placed on the route of a seat they hold: undo its end (a leave or a move changed
    their mind), if the seat is still free after it; a move still to come ends at once.
    409 ALREADY_ON_ROUTE when that seat isn't ending."""
    if staying.end_date is None:
        raise AppError(ErrorCode.ALREADY_ON_ROUTE)
    await seats.full_or_raise(session, route, staying.end_date + timedelta(days=1))
    for seat in held:
        if seat.id != staying.id and seat.start_date > today:
            await _end_seat(session, caller, seat, last_day=seat.start_date - timedelta(days=1))
    reopened = await session.get_one(RouteAssignment, staying.id)
    before = audit.snapshot(ROUTE_ASSIGNMENT_AUDIT, reopened)
    reopened.end_date = None
    await session.flush()
    audit.record(
        session,
        actor_id=caller.id,
        action="route_assignment.changed",
        entity=ROUTE_ASSIGNMENT_AUDIT,
        entity_id=reopened.id,
        before=before,
        after=audit.snapshot(ROUTE_ASSIGNMENT_AUDIT, reopened),
    )


async def _company_takes_riders(session: AsyncSession, company_id: uuid.UUID) -> None:
    """A company rider's company is live and active, share-locked (a deactivation waits);
    409 ROUTE_NOT_READY otherwise, as for its routes (GZ-024)."""
    company = await get_live_company(session, company_id, lock="share")
    if company is None or not company.is_active:
        raise AppError(ErrorCode.ROUTE_NOT_READY)


async def end(
    session: AsyncSession,
    caller: CurrentStaff,
    rider_id: uuid.UUID,
    ending: SeatEnd,
    idempotent: IdempotentRequest,
) -> Response:
    """End what the rider holds: 200 with the rider, or the stored response for a retry.
    409 RIDER_HAS_NO_SEAT when they hold none."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        row = await _live(session, rider_id, for_update=True)
        today = schedule.addis_today()
        held, _ = await _lock_seat_routes(session, row.id, today)
        if not held:
            raise AppError(ErrorCode.RIDER_HAS_NO_SEAT)
        last_day = _last_day(row, ending.end_date, today)
        for seat in held:
            await _end_seat(session, caller, seat, last_day=last_day)
        payload = (await _read_with_seats(session, row)).model_dump(mode="json")
        await record(session, claimed, status=200, body=payload)
    return JSONResponse(payload, status_code=200)


async def delete(session: AsyncSession, caller: CurrentStaff, rider_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role). 409 RIDER_HAS_SEAT while they hold
    a seat today or later; their open requests stay as they are."""
    async with session.begin():
        row = await _live(session, rider_id, for_update=True)
        if await repository.held_seats(session, [row.id], schedule.addis_today()):
            raise AppError(ErrorCode.RIDER_HAS_SEAT)
        before = audit.snapshot(RIDER_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="rider.deleted",
            entity=RIDER_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(RIDER_AUDIT, row),
        )


async def _lock_seat_routes(
    session: AsyncSession, rider_id: uuid.UUID, today: date, *, also: uuid.UUID | None = None
) -> tuple[list[HeldSeat], list[Route]]:
    """Lock the routes of the seats the rider (locked already) holds, and `also`, in a
    fixed order; then read the seats again, since a launch may have lapsed one meanwhile.
    Returns the seats and the live routes locked that still matter (`also` when live)."""
    held = (await repository.held_seats(session, [rider_id], today)).get(rider_id, [])
    ids = sorted({seat.route_id for seat in held} | ({also} if also is not None else set()))
    locked = [await routes.get_live_route(session, route_id, for_update=True) for route_id in ids]
    held = (await repository.held_seats(session, [rider_id], today)).get(rider_id, [])
    still = {seat.route_id for seat in held} | {also}
    return held, [route for route in locked if route is not None and route.id in still]


async def _takes(session: AsyncSession, route: Route, row: Rider) -> bool:
    """The route takes this rider: an adult on a forming or active public route with
    prices, on their trip, Saturday where they want it; a company rider on their company's
    route, boarding where it passes."""
    if row.rider_type is RiderType.CORPORATE:
        return (
            route.route_type is RouteType.CORPORATE
            and route.company_id == row.company_id
            and row.home_area_id is not None
            and await routes.serves(
                session, route.id, row.home_area_id, route.corridor.destination_area_id
            )
        )
    return (
        route.route_type is RouteType.PUBLIC
        and route.status in (RouteStatus.FORMING, RouteStatus.ACTIVE)
        and route.price_round_trip is not None
        and route.price_one_way is not None
        and (not row.saturday_addon or route.price_saturday is not None)
        and row.home_area_id is not None
        and row.destination_area_id is not None
        and await routes.serves(session, route.id, row.home_area_id, row.destination_area_id)
    )


def _start(row: Rider, requested: date | None, today: date) -> date:
    """An adult from a 1st, the next by default; a company rider any day, today by
    default; never past, at most a year ahead (a 422 on `start_date` otherwise)."""
    if row.rider_type is RiderType.CORPORATE:
        start = requested or today
        if not today <= start <= today + _A_YEAR:
            raise invalid_field("body", "start_date")
        return start
    earliest = routes.next_launch_day(today)
    start = requested or earliest
    if start.day != 1 or not earliest <= start <= date(today.year + 1, today.month, 1):
        raise invalid_field("body", "start_date")
    return start


def _last_day(row: Rider, requested: date | None, today: date) -> date:
    """An adult leaves at a month's end, this month's by default; a company rider any day,
    today by default; never past, at most a year ahead (a 422 on `end_date` otherwise)."""
    if row.rider_type is RiderType.CORPORATE:
        last = requested or today
    else:
        last = requested or routes.first_of_next_month(today) - timedelta(days=1)
        if (last + timedelta(days=1)).day != 1:
            raise invalid_field("body", "end_date")
    if not today <= last <= today + _A_YEAR:
        raise invalid_field("body", "end_date")
    return last


async def _queue(
    session: AsyncSession, route: Route, row: Rider, skip_reason: str | None
) -> tuple[list[uuid.UUID], str | None]:
    """Placing an adult on an active route: other people's open requests for it made
    before the rider's own come first (a free seat goes to the oldest request). Returns
    the rider's own requests (to fulfil) and the skip reason when it was needed; 409
    ROUTE_HAS_WAITING_REQUESTS without one."""
    own = await route_requests.own_requests_for(
        session, route.id, rider_id=row.id, phone=row.phone, name=row.full_name
    )
    waiting = await route_requests.others_waiting(
        session,
        route.id,
        exclude=[request.id for request in own],
        before=own[0].created_at if own else None,
    )
    if waiting and skip_reason is None:
        raise AppError(ErrorCode.ROUTE_HAS_WAITING_REQUESTS)
    return [request.id for request in own], skip_reason if waiting else None


async def _fulfil(
    session: AsyncSession, caller: CurrentStaff, request_ids: list[uuid.UUID]
) -> None:
    """The rider's own open requests for the route they now hold (locked by `_queue`)."""
    for request_id in request_ids:
        request = await session.get_one(RouteRequest, request_id)
        before = audit.snapshot(ROUTE_REQUEST_AUDIT, request)
        request.status = RequestStatus.FULFILLED
        await session.flush()
        audit.record(
            session,
            actor_id=caller.id,
            action="route_request.status_changed",
            entity=ROUTE_REQUEST_AUDIT,
            entity_id=request.id,
            before=before,
            after=audit.snapshot(ROUTE_REQUEST_AUDIT, request),
        )


async def _end_seat(
    session: AsyncSession, caller: CurrentStaff, held: HeldSeat, *, last_day: date
) -> None:
    """The seat ends after `last_day`: a reservation, or a seat starting later, just ends
    (it never rode); one ending earlier already keeps its date."""
    seat = (
        await session.scalars(select(RouteAssignment).where(RouteAssignment.id == held.id))
    ).one()
    before = audit.snapshot(ROUTE_ASSIGNMENT_AUDIT, seat)
    if seat.status is AssignmentStatus.RESERVED or last_day < seat.start_date:
        seat.status = AssignmentStatus.ENDED
    elif seat.end_date is None or seat.end_date > last_day:
        seat.end_date = last_day
    await session.flush()
    audit.record(
        session,
        actor_id=caller.id,
        action="route_assignment.changed",
        entity=ROUTE_ASSIGNMENT_AUDIT,
        entity_id=seat.id,
        before=before,
        after=audit.snapshot(ROUTE_ASSIGNMENT_AUDIT, seat),
    )


async def _still_on_the_way(
    session: AsyncSession,
    route: Route,
    row: Rider,
    home: uuid.UUID | None,
    destination: uuid.UUID | None,
    moved: list[str],
) -> None:
    """The route still takes the rider: boarding at A or an area it serves, going to B
    (the route's own B for a company rider). Otherwise a 422 on the area that breaks it:
    with both changed, the new home is judged against the current destination first."""
    if row.rider_type is RiderType.CORPORATE:
        destination = route.corridor.destination_area_id
    if (
        home is not None
        and destination is not None
        and await routes.serves(session, route.id, home, destination)
    ):
        return
    field = moved[0]
    if (
        len(moved) == len(_AREA_FIELDS)
        and home is not None
        and row.destination_area_id is not None
        and await routes.serves(session, route.id, home, row.destination_area_id)
    ):
        field = "destination_area_id"
    raise invalid_field("body", field)


async def _refer_to_areas(session: AsyncSession, areas: dict[str, uuid.UUID | None]) -> None:
    """Share-lock each named area, in a fixed order; unknown or deleted is a 422 on its
    field."""
    for area_id, field in sorted(
        (area_id, field) for field, area_id in areas.items() if area_id is not None
    ):
        if await get_live_area(session, area_id, lock="share") is None:
            raise invalid_field("body", field)


async def _live(session: AsyncSession, rider_id: uuid.UUID, *, for_update: bool = False) -> Rider:
    row = await repository.get_live_rider(session, rider_id, for_update=for_update)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _listed(session: AsyncSession, rows: list[Rider], today: date) -> list[RiderListItem]:
    ids = [row.id for row in rows]
    held = await repository.held_seats(session, ids, today)
    requests = await repository.open_request_counts(session, ids)
    return [
        RiderListItem.model_validate(
            {
                **_read(row, held.get(row.id, []), requests.get(row.id, 0)).model_dump(
                    exclude={"phone"}
                ),
                "phone_masked": masked(row.phone),
            }
        )
        for row in rows
    ]


async def _read_with_seats(session: AsyncSession, row: Rider) -> RiderRead:
    held = (await repository.held_seats(session, [row.id], schedule.addis_today())).get(row.id, [])
    requests = (await repository.open_request_counts(session, [row.id])).get(row.id, 0)
    return _read(row, held, requests)


def _read(row: Rider, held: list[HeldSeat], open_requests: int) -> RiderRead:
    seat_refs = [
        SeatRef(
            route_id=seat.route_id,
            route_code=seat.route_code,
            status=seat.status,
            start_date=seat.start_date,
            end_date=seat.end_date,
        )
        for seat in held
    ]
    return RiderRead(
        id=row.id,
        rider_type=row.rider_type,
        full_name=row.full_name,
        phone=row.phone,
        home_area=None if row.home_area is None else AreaRef.model_validate(row.home_area),
        destination_area=(
            None if row.destination_area is None else AreaRef.model_validate(row.destination_area)
        ),
        plan=row.plan,
        saturday_addon=row.saturday_addon,
        source=row.source,
        company=None if row.company is None else CompanyRef.model_validate(row.company),
        seat=seat_refs[0] if seat_refs else None,
        next_seat=seat_refs[1] if len(seat_refs) > 1 else None,
        open_requests=open_requests,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def flush_riders(session: AsyncSession) -> None:
    """Flush; a phone another request took meanwhile is the same 409 as the check's (also
    for a rider made from an accepted offer, GZ-040)."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _rider_exists_or_raise(exc)


def _rider_exists_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) in (RIDER_ADULT_PHONE_UNIQUE, RIDER_COMPANY_PHONE_UNIQUE):
        raise AppError(ErrorCode.RIDER_EXISTS) from None
    raise exc
