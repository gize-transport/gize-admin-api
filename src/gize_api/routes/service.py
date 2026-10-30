"""Route business rules (GZ-021). Any active staff member creates, lists, reads and edits
routes; only admins set prices (GZ-022). Status and deletion (GZ-024) have their own
endpoints. Every change is audited.

A route refers to a live corridor and, for corporate routes, a live active company: it
share-locks them while it is created or moved, and deactivating or deleting them takes an
update lock, so the two never cross. Seats are checked under the route's own row lock, the
lock M3 takes to add a rider.
"""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.companies.models import Company
from gize_api.companies.repository import get_live_company
from gize_api.core import audit, schedule
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.corridors.models import Corridor
from gize_api.corridors.repository import get_live_corridor
from gize_api.routes import repository
from gize_api.routes.models import ROUTE_AUDIT, ROUTE_CODE_UNIQUE, Route, RouteType
from gize_api.routes.repository import SeatCounts
from gize_api.routes.schemas import (
    CompanyRef,
    CorridorRef,
    MonthSeats,
    Prices,
    RouteCreate,
    RouteListParams,
    RoutePage,
    RoutePricesSet,
    RouteRead,
    RouteUpdate,
)


async def create(
    session: AsyncSession, caller: CurrentStaff, route: RouteCreate, idempotent: IdempotentRequest
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        corridor = await _corridor_to_refer_to(session, route.corridor_id)
        company = None
        if route.company_id is not None:
            company = await _company_to_refer_to(session, route.company_id)
        if await repository.code_taken(session, route.code, except_id=None):
            raise AppError(ErrorCode.ROUTE_CODE_TAKEN)
        row = Route(
            code=route.code,
            corridor=corridor,
            route_type=route.route_type,
            school_name=route.school_name,
            company=company,
            morning_time=route.morning_time,
            return_time=route.return_time,
            seats=route.seats,
            launch_minimum=route.launch_minimum,
        )
        session.add(row)
        await _flush(session)
        await session.refresh(row, ["status", "created_at", "updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.created",
            entity=ROUTE_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(ROUTE_AUDIT, row),
        )
        payload = _read(row, SeatCounts()).model_dump(mode="json")
        location = f"/v1/routes/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_routes(session: AsyncSession, params: RouteListParams) -> RoutePage:
    """Newest first, with places and seat counts (three queries, however long the page)."""
    async with session.begin():
        page = await paginate(
            session,
            repository.live_routes(
                q=params.q,
                route_type=params.route_type,
                status=params.status,
                corridor_id=params.corridor_id,
                company_id=params.company_id,
            ),
            params,
            created_at=Route.created_at,
            id=Route.id,
            newest_first=True,
        )
        counts = await repository.seat_counts(
            session, [route.id for route in page.items], schedule.addis_today()
        )
        return RoutePage(
            items=[_read(route, counts[route.id]) for route in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, route_id: uuid.UUID) -> RouteRead:
    async with session.begin():
        row = await _live(session, route_id)
        return await _read_with_counts(session, row)


async def update(
    session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID, change: RouteUpdate
) -> RouteRead:
    """Setting what is already there changes and records nothing."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        today = schedule.addis_today()
        _fits_the_type(row.route_type, change)
        morning = _new(change.morning_time, row.morning_time)
        return_time = _new(change.return_time, row.return_time)
        if morning >= return_time:
            raise invalid_field("body")
        # Places first: one that doesn't exist is a 422, whatever else is refused.
        corridor, company = row.corridor, row.company
        if change.corridor_id not in (None, row.corridor_id):
            corridor = await _corridor_to_refer_to(session, change.corridor_id)
        if change.company_id not in (None, row.company_id):
            company = await _company_to_refer_to(session, change.company_id)
        seats = _new(change.seats, row.seats)
        minimum = _new(change.launch_minimum, row.launch_minimum)
        if minimum is not None and minimum > seats:
            raise AppError(ErrorCode.SEATS_BELOW_LAUNCH_MINIMUM)
        if seats < row.seats and seats < await repository.peak_taken_from(session, row.id, today):
            raise AppError(ErrorCode.SEATS_BELOW_TAKEN)
        if change.code not in (None, row.code) and await repository.code_taken(
            session, change.code, except_id=row.id
        ):
            raise AppError(ErrorCode.ROUTE_CODE_TAKEN)
        moves = (
            change.corridor_id not in (None, row.corridor_id)
            or change.school_name not in (None, row.school_name)
            or change.company_id not in (None, row.company_id)
        )
        if moves and await repository.has_riders(session, row.id, today):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)

        before = audit.snapshot(ROUTE_AUDIT, row)
        row.corridor, row.company = corridor, company
        for field in ("code", "school_name", "seats", "launch_minimum"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        row.morning_time, row.return_time = morning, return_time
        await _flush(session)
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.updated",
            entity=ROUTE_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_AUDIT, row),
        )
        return await _read_with_counts(session, row)


async def set_prices(
    session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID, prices: RoutePricesSet
) -> RouteRead:
    """Admins only (the route's require_role). Setting the prices already there records
    nothing. When a change takes effect for riders is billing's concern (later)."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        if row.route_type is RouteType.CORPORATE:
            raise AppError(ErrorCode.ROUTE_HAS_NO_PRICES)  # billed per company
        if prices.saturday is not None and row.route_type is not RouteType.PUBLIC:
            raise invalid_field("body", "saturday")
        before = audit.snapshot(ROUTE_AUDIT, row)
        row.price_round_trip = prices.round_trip
        row.price_one_way = prices.one_way
        row.price_saturday = prices.saturday
        await session.flush()
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.prices_changed",
            entity=ROUTE_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_AUDIT, row),
        )
        return await _read_with_counts(session, row)


def _new[T](change: T | None, current: T) -> T:
    return current if change is None else change


def _fits_the_type(route_type: RouteType, change: RouteUpdate) -> None:
    """Fields a route of this type doesn't have are refused (422 on the field)."""
    not_for_this_type = (
        ("school_name", route_type is not RouteType.STUDENT),
        ("company_id", route_type is not RouteType.CORPORATE),
        ("launch_minimum", route_type is RouteType.CORPORATE),
    )
    for field, refused in not_for_this_type:
        if refused and field in change.model_fields_set:
            raise invalid_field("body", field)


async def _live(session: AsyncSession, route_id: uuid.UUID, *, for_update: bool = False) -> Route:
    row = await repository.get_live_route(session, route_id, for_update=for_update)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _corridor_to_refer_to(session: AsyncSession, corridor_id: uuid.UUID) -> Corridor:
    corridor = await get_live_corridor(session, corridor_id, lock="share")
    if corridor is None:
        raise invalid_field("body", "corridor_id")
    return corridor


async def _company_to_refer_to(session: AsyncSession, company_id: uuid.UUID) -> Company:
    """A live, active company, share-locked: it can't be deactivated or deleted meanwhile."""
    company = await get_live_company(session, company_id, lock="share")
    if company is None or not company.is_active:
        raise invalid_field("body", "company_id")
    return company


async def _read_with_counts(session: AsyncSession, row: Route) -> RouteRead:
    counts = await repository.seat_counts(session, [row.id], schedule.addis_today())
    return _read(row, counts[row.id])


def _read(row: Route, counts: SeatCounts) -> RouteRead:
    taken = MonthSeats(this_month=counts.this_month, next_month=counts.next_month)
    return RouteRead(
        id=row.id,
        code=row.code,
        corridor=CorridorRef.model_validate(row.corridor),
        route_type=row.route_type,
        school_name=row.school_name,
        company=None if row.company is None else CompanyRef.model_validate(row.company),
        morning_time=row.morning_time,
        return_time=row.return_time,
        seats=row.seats,
        launch_minimum=row.launch_minimum,
        prices=(
            None
            if row.route_type is RouteType.CORPORATE
            else Prices(
                round_trip=row.price_round_trip,
                one_way=row.price_one_way,
                saturday=row.price_saturday,
            )
        ),
        status=row.status,
        copied_from_route_id=row.copied_from_route_id,
        seats_taken=taken,
        seats_free=MonthSeats(
            this_month=max(row.seats - taken.this_month, 0),
            next_month=max(row.seats - taken.next_month, 0),
        ),
        reserved=counts.reserved,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _flush(session: AsyncSession) -> None:
    """Flush; a code another request took meanwhile is the same 409 as the check's."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _code_taken_or_raise(exc)


def _code_taken_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == ROUTE_CODE_UNIQUE:
        raise AppError(ErrorCode.ROUTE_CODE_TAKEN) from None
    raise exc
