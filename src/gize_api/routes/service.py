"""Route business rules (GZ-021). Any active staff member creates, lists, reads and edits
routes and moves them through their statuses (GZ-024); only admins set prices (GZ-022) and
delete routes. Every change is audited.

A route refers to a live corridor and, for corporate routes, a live active company: it
share-locks them while it is created or moved, and deactivating or deleting them takes an
update lock, so the two never cross. Seats are checked under the route's own row lock, the
lock M3 takes to add a rider: a launch counts its reservations, and a delete or a return
to draft looks for riders, with none joining meanwhile.
"""

import uuid
from datetime import date
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.areas.repository import get_live_area
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
from gize_api.corridors.schemas import AreaRef
from gize_api.drivers import repository as drivers
from gize_api.route_requests.repository import names_route as request_names_route
from gize_api.routes import repository
from gize_api.routes.models import (
    ROUTE_AREAS_AUDIT,
    ROUTE_AUDIT,
    ROUTE_CODE_UNIQUE,
    ROUTE_DRIVER_AUDIT,
    ROUTE_LAUNCH_AUDIT,
    Route,
    RouteArea,
    RouteStatus,
    RouteType,
)
from gize_api.routes.repository import SeatCounts
from gize_api.routes.schemas import (
    CompanyRef,
    CorridorRef,
    DriverRef,
    LaunchMonth,
    MonthSeats,
    Prices,
    RouteAreasSet,
    RouteBlocker,
    RouteCopy,
    RouteCreate,
    RouteDriverSet,
    RouteLaunchMonths,
    RouteListParams,
    RoutePage,
    RoutePricesSet,
    RouteRead,
    RouteStatusSet,
    RouteUpdate,
    StatusChange,
    times_in_order,
)

DRAFT, FORMING, ACTIVE, PAUSED = (
    RouteStatus.DRAFT,
    RouteStatus.FORMING,
    RouteStatus.ACTIVE,
    RouteStatus.PAUSED,
)
# The moves each kind of route may make (owner decisions, GZ-025): prepaid individuals'
# routes form first; a corporate route starts from the company's agreement.
_MOVES = {
    "individual": {(DRAFT, FORMING), (FORMING, DRAFT), (FORMING, ACTIVE), (ACTIVE, PAUSED),
                   (PAUSED, ACTIVE)},
    "corporate": {(DRAFT, ACTIVE), (ACTIVE, PAUSED), (PAUSED, ACTIVE)},
}  # fmt: skip


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
            morning_arrival_time=route.morning_arrival_time,
            return_time=route.return_time,
            return_arrival_time=route.return_arrival_time,
            seats=route.seats,
            launch_minimum=route.launch_minimum,
            route_areas=[],  # set later (PUT .../areas); never loaded lazily
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
        payload = _read(row, SeatCounts(), schedule.addis_today()).model_dump(mode="json")
        location = f"/v1/routes/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def copy(
    session: AsyncSession,
    caller: CurrentStaff,
    route_id: uuid.UUID,
    van: RouteCopy,
    idempotent: IdempotentRequest,
) -> Response:
    """Add a van: a new route like this one (GZ-028). 201 with a Location, or the stored
    response for a retry. The original is share-locked, so its served areas and prices are
    copied whole, as they are before or after any change to them."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        if await repository.refer_to_live_route(session, route_id) is None:
            raise AppError(ErrorCode.NOT_FOUND)
        original = await _live(session, route_id)  # now with its places
        corporate = original.route_type is RouteType.CORPORATE
        if (van.launch_minimum is None) != corporate:
            raise invalid_field("body", "launch_minimum")  # individual routes only, required
        company = None
        if original.company_id is not None:
            company = await get_live_company(session, original.company_id, lock="share")
            if company is None or not company.is_active:
                raise AppError(ErrorCode.ROUTE_NOT_READY)
        if await repository.code_taken(session, van.code, except_id=None):
            raise AppError(ErrorCode.ROUTE_CODE_TAKEN)
        priced = None not in (original.price_round_trip, original.price_one_way)
        row = Route(
            code=van.code,
            corridor=original.corridor,
            route_type=original.route_type,
            school_name=original.school_name,
            company=company,
            morning_time=original.morning_time,
            morning_arrival_time=original.morning_arrival_time,
            return_time=original.return_time,
            return_arrival_time=original.return_arrival_time,
            seats=van.seats,
            launch_minimum=van.launch_minimum,
            price_round_trip=original.price_round_trip,
            price_one_way=original.price_one_way,
            price_saturday=original.price_saturday,
            # Ready to collect reservations if the original could form; corporate routes
            # start from the company's agreement.
            status=FORMING if priced and not corporate else DRAFT,
            copied_from_route_id=original.id,
            route_areas=[
                RouteArea(area=served.area, position=served.position)
                for served in original.route_areas
            ],
        )
        session.add(row)
        await _flush(session)
        await session.refresh(row, ["created_at", "updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.copied",
            entity=ROUTE_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(ROUTE_AUDIT, row),
        )
        if row.route_areas:
            audit.record(
                session,
                actor_id=caller.id,
                action="route.areas_changed",
                entity=ROUTE_AREAS_AUDIT,
                entity_id=row.id,
                before={"served_area_ids": []},
                after={"served_area_ids": [served.area_id for served in row.route_areas]},
            )
        payload = _read(row, SeatCounts(), schedule.addis_today()).model_dump(mode="json")
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
        today = schedule.addis_today()
        counts = await repository.seat_counts(session, [route.id for route in page.items], today)
        return RoutePage(
            items=[_read(route, counts[route.id], today) for route in page.items],
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
        times = (
            _new(change.morning_time, row.morning_time),
            _new(change.morning_arrival_time, row.morning_arrival_time),
            _new(change.return_time, row.return_time),
            _new(change.return_arrival_time, row.return_arrival_time),
        )
        try:
            times_in_order(*times)
        except ValueError:
            raise invalid_field("body") from None
        # A route made before arrival times existed gets both at once, never one.
        for field, own, other in (
            ("morning_arrival_time", times[1], times[3]),
            ("return_arrival_time", times[3], times[1]),
        ):
            if own is None and other is not None:
                raise invalid_field("body", field)
        # Places first: one that doesn't exist is a 422, whatever else is refused.
        corridor, company = row.corridor, row.company
        if change.corridor_id not in (None, row.corridor_id):
            if row.route_areas:  # they lie between this corridor's A and B: clear them first
                raise invalid_field("body", "corridor_id")
            corridor = await _corridor_to_refer_to(session, change.corridor_id)
        if change.company_id not in (None, row.company_id):
            company = await _company_to_refer_to(session, change.company_id)
        seats = _new(change.seats, row.seats)
        minimum = _new(change.launch_minimum, row.launch_minimum)
        if minimum is not None and minimum > seats:
            raise AppError(ErrorCode.SEATS_BELOW_LAUNCH_MINIMUM)
        if seats < row.seats and seats < await repository.taken_with_offers(
            session, row.id, today, excluding=None
        ):  # open offers (GZ-040) hold seats too
            raise AppError(ErrorCode.SEATS_BELOW_TAKEN)
        if change.code not in (None, row.code) and await repository.code_taken(
            session, change.code, except_id=row.id
        ):
            raise AppError(ErrorCode.ROUTE_CODE_TAKEN)
        # A school spelled in another case is the same school (requests and riders match it
        # ignoring case): fixing the case moves nobody.
        new_school = change.school_name is not None and (
            row.school_name is None or change.school_name.casefold() != row.school_name.casefold()
        )
        relocates = change.corridor_id not in (None, row.corridor_id) or new_school
        moves = relocates or change.company_id not in (None, row.company_id)
        if moves and await repository.has_riders(session, row.id, today):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)
        # Where it runs and whom it serves are frozen once it is priced or asked for (owner
        # decision, after M2): to serve somewhere else, add a route. Requests share-lock the
        # route, so none is recorded or matched while this change holds its lock.
        if relocates and await request_names_route(session, row.id):
            raise AppError(ErrorCode.ROUTE_HAS_REQUESTS)
        priced = (row.price_round_trip, row.price_one_way, row.price_saturday) != (None,) * 3
        if relocates and priced:
            raise AppError(ErrorCode.ROUTE_HAS_PRICES)
        if row.driver_id is not None and times != (
            row.morning_time,
            row.morning_arrival_time,
            row.return_time,
            row.return_arrival_time,
        ):  # its driver's other routes must still fit (GZ-053; route, then driver locked)
            await _driver_free_or_raise(
                session, row.driver_id, times, route_id=row.id, assigning=False
            )

        before = audit.snapshot(ROUTE_AUDIT, row)
        row.corridor, row.company = corridor, company
        for field in ("code", "school_name", "seats", "launch_minimum"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        row.morning_time, row.morning_arrival_time, row.return_time, row.return_arrival_time = times
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
        today = schedule.addis_today()
        if (
            prices.saturday is None
            and row.price_saturday is not None
            and await repository.saturday_held(session, row.id, today)
        ):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)  # riders hold the add-on
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


async def set_areas(
    session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID, areas: RouteAreasSet
) -> RouteRead:
    """Replace the areas the route serves. The same list records nothing."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        ends = {row.corridor.origin_area_id, row.corridor.destination_area_id}
        if len(set(areas.area_ids)) != len(areas.area_ids) or ends & set(areas.area_ids):
            raise invalid_field("body", "area_ids")  # each once; A and B are the corridor's
        before = [served.area_id for served in row.route_areas]
        if before == areas.area_ids:
            return await _read_with_counts(session, row)
        dropped = set(before) - set(areas.area_ids)
        if dropped and await repository.boarded_in(
            session, row.id, dropped, schedule.addis_today()
        ):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)  # riders board there
        served = [await _area_to_refer_to(session, area_id) for area_id in areas.area_ids]
        for old in row.route_areas:
            await session.delete(old)
        await session.flush()
        row.route_areas = [
            RouteArea(route_id=row.id, area=area, position=place)
            for place, area in enumerate(served, start=1)
        ]
        row.updated_at = func.now()  # the route changed, though none of its own columns did
        await session.flush()
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.areas_changed",
            entity=ROUTE_AREAS_AUDIT,
            entity_id=row.id,
            before={"served_area_ids": before},
            after={"served_area_ids": list(areas.area_ids)},
        )
        return await _read_with_counts(session, row)


async def set_driver(
    session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID, body: RouteDriverSet
) -> RouteRead:
    """Give the route its driver, replace them, or clear it (GZ-053). Locks the route,
    then the driver: two routes given one driver at once are judged in turn."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        before = {"driver_id": row.driver_id}
        if body.driver_id is not None and body.driver_id != row.driver_id:
            times = (
                row.morning_time, row.morning_arrival_time, row.return_time,
                row.return_arrival_time,
            )  # fmt: skip
            await _driver_free_or_raise(
                session, body.driver_id, times, route_id=row.id, assigning=True
            )
        row.driver_id = body.driver_id
        await session.flush()
        row = await _live(session, route_id)  # with the driver loaded
        audit.record(
            session,
            actor_id=caller.id,
            action="route.driver_changed",
            entity=ROUTE_DRIVER_AUDIT,
            entity_id=row.id,
            before=before,
            after={"driver_id": row.driver_id},
        )
        return await _read_with_counts(session, row)


async def _driver_free_or_raise(
    session: AsyncSession,
    driver_id: uuid.UUID,
    times: drivers.Times,
    *,
    route_id: uuid.UUID,
    assigning: bool,
) -> None:
    """The driver, locked, is live and active when `assigning` (422 on `driver_id`), and
    none of their other routes' runs overlaps these times (409 DRIVER_BUSY). New times on
    a route keep its driver: only the overlap is judged then."""
    driver = await drivers.get_live_driver(session, driver_id, for_update=True)
    if assigning and (driver is None or not driver.is_active):
        raise invalid_field("body", "driver_id")
    if await drivers.busy(session, driver_id, times, excluding=route_id):
        raise AppError(ErrorCode.DRIVER_BUSY)


async def launch_months(session: AsyncSession, route_id: uuid.UUID) -> RouteLaunchMonths:
    """Each day a launch may name (`set_status`'s window), with the riders it would start."""
    today = schedule.addis_today()
    days = [repository.next_launch_day(today)]
    while (later := repository.first_of_next_month(days[-1])) <= _latest_launch(today):
        days.append(later)
    async with session.begin():
        row = await _live(session, route_id)
        counts = await repository.reserved_by_month(session, row.id, days)
    forming, minimum = row.status is FORMING, row.launch_minimum or 0  # as set_status
    return RouteLaunchMonths(
        status=row.status,
        launch_minimum=row.launch_minimum,
        months=[
            LaunchMonth(launch_date=day, reserved=count, ready=forming and count >= minimum)
            for day, count in zip(days, counts, strict=True)
        ],
    )


async def set_status(
    session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID, change: RouteStatusSet
) -> RouteRead:
    """Move the route; the status it already has changes and records nothing (so a retried
    launch is harmless, whatever its date)."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        if change.status is row.status:
            return await _read_with_counts(session, row)
        move = (row.status, change.status)
        kind = "corporate" if row.route_type is RouteType.CORPORATE else "individual"
        if move not in _MOVES[kind]:
            raise AppError(ErrorCode.INVALID_STATUS_CHANGE)
        launch_date = change.launch_date
        launching = move == (FORMING, ACTIVE)
        if launching != (launch_date is not None):
            raise invalid_field("body", "launch_date")  # a launch, and only a launch, has one
        today = schedule.addis_today()
        if launch_date is not None and not today <= launch_date <= _latest_launch(today):
            raise invalid_field("body", "launch_date")
        if change.status is FORMING and None in (row.price_round_trip, row.price_one_way):
            raise AppError(ErrorCode.ROUTE_NOT_READY)
        if move == (FORMING, DRAFT) and await repository.has_riders(session, row.id, today):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)  # the same rule as a delete's
        if kind == "corporate" and change.status is ACTIVE:
            await _company_still_active(session, row)

        before = audit.snapshot(ROUTE_AUDIT, row)
        started = lapsed = 0
        if launch_date is not None:
            minimum = row.launch_minimum or 0  # set on every individual route (a table check)
            if await repository.reserved_for(session, row.id, launch_date) < minimum:
                raise AppError(ErrorCode.NOT_ENOUGH_RIDERS)
            started, lapsed = await repository.start_reserved(session, row.id, launch_date)
        row.status = change.status
        await session.flush()
        await session.refresh(row, ["updated_at"])
        if launch_date is not None:
            audit.record(
                session,
                actor_id=caller.id,
                action="route.launched",
                entity=ROUTE_LAUNCH_AUDIT,
                entity_id=row.id,
                before={"status": FORMING, "launch_date": None, "riders_started": None,
                        "lapsed": None},
                after={"status": ACTIVE, "launch_date": launch_date, "riders_started": started,
                       "lapsed": lapsed},
            )  # fmt: skip
        else:
            audit.record(
                session,
                actor_id=caller.id,
                action="route.status_changed",
                entity=ROUTE_AUDIT,
                entity_id=row.id,
                before=before,
                after=audit.snapshot(ROUTE_AUDIT, row),
            )
        return await _read_with_counts(session, row)


async def delete(session: AsyncSession, caller: CurrentStaff, route_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role): its code is free again; history and
    riders' past seats keep it. Refused while anyone is reserved or rides today or later."""
    async with session.begin():
        row = await _live(session, route_id, for_update=True)
        if await repository.has_riders(session, row.id, schedule.addis_today()):
            raise AppError(ErrorCode.ROUTE_HAS_RIDERS)
        if await request_names_route(session, row.id):  # requests share-lock it (GZ-027)
            raise AppError(ErrorCode.ROUTE_HAS_REQUESTS)
        before = audit.snapshot(ROUTE_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route.deleted",
            entity=ROUTE_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_AUDIT, row),
        )


def _latest_launch(today: date) -> date:
    """A launch at most a year ahead (a bound on the input: a mistyped year would move every
    reservation decades on)."""
    return date(today.year + 1, today.month, 1)


async def _company_still_active(session: AsyncSession, row: Route) -> None:
    """A corporate route runs only for an active company. The share lock makes a company
    deactivation (which takes an update lock, then looks for running routes) wait for this
    change, or this change wait for it and see the company inactive."""
    company = (
        None
        if row.company_id is None
        else await get_live_company(session, row.company_id, lock="share")
    )
    if company is None or not company.is_active:
        raise AppError(ErrorCode.ROUTE_NOT_READY)


def _status_changes(row: Route, counts: SeatCounts, today: date) -> list[StatusChange]:
    """The moves `set_status` allows from the route's status, each with what would refuse
    it now (the same checks, in the same order). A launch is judged for the earliest day it
    may name, the day `counts.reserved` is for."""
    kind = "corporate" if row.route_type is RouteType.CORPORATE else "individual"
    changes = []
    for to in RouteStatus:
        move = (row.status, to)
        if move not in _MOVES[kind]:
            continue
        blocked_by = []
        if to is FORMING and None in (row.price_round_trip, row.price_one_way):
            blocked_by.append(RouteBlocker.PRICES_MISSING)
        if move == (FORMING, DRAFT) and counts.held:
            blocked_by.append(RouteBlocker.ROUTE_HAS_RIDERS)
        company = row.company
        inactive = company is None or company.deleted_at is not None or not company.is_active
        if kind == "corporate" and to is ACTIVE and inactive:
            blocked_by.append(RouteBlocker.COMPANY_INACTIVE)
        launch_date = None
        if move == (FORMING, ACTIVE):
            launch_date = repository.next_launch_day(today)
            if counts.reserved < (row.launch_minimum or 0):
                blocked_by.append(RouteBlocker.NOT_ENOUGH_RIDERS)
        changes.append(
            StatusChange(
                to=to, ready=not blocked_by, blocked_by=blocked_by, launch_date=launch_date
            )
        )
    return changes


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


async def _area_to_refer_to(session: AsyncSession, area_id: uuid.UUID) -> Area:
    area = await get_live_area(session, area_id, lock="share")
    if area is None:
        raise invalid_field("body", "area_ids")
    return area


async def _company_to_refer_to(session: AsyncSession, company_id: uuid.UUID) -> Company:
    """A live, active company, share-locked: it can't be deactivated or deleted meanwhile."""
    company = await get_live_company(session, company_id, lock="share")
    if company is None or not company.is_active:
        raise invalid_field("body", "company_id")
    return company


async def _read_with_counts(session: AsyncSession, row: Route) -> RouteRead:
    today = schedule.addis_today()
    counts = await repository.seat_counts(session, [row.id], today)
    return _read(row, counts[row.id], today)


def _read(row: Route, counts: SeatCounts, today: date) -> RouteRead:
    taken = MonthSeats(this_month=counts.this_month, next_month=counts.next_month)
    return RouteRead(
        id=row.id,
        code=row.code,
        corridor=CorridorRef.model_validate(row.corridor),
        route_type=row.route_type,
        school_name=row.school_name,
        company=None if row.company is None else CompanyRef.model_validate(row.company),
        driver=None if row.driver_id is None else DriverRef.model_validate(row.driver),
        morning_time=row.morning_time,
        morning_arrival_time=row.morning_arrival_time,
        return_time=row.return_time,
        return_arrival_time=row.return_arrival_time,
        served_areas=[AreaRef.model_validate(served.area) for served in row.route_areas],
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
        status_changes=_status_changes(row, counts, today),
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
