"""Route request business rules (GZ-027). Any active staff member records, lists, reads,
corrects and moves requests; only admins delete them (soft). Every change is audited, with
personal data redacted.

A request refers to live areas and routes: it share-locks them while it is recorded or
matched, and deleting an area or route takes an update lock, then refuses while an open
request names it, so the two never cross.
"""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.areas.repository import get_live_area
from gize_api.core import audit, schedule
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.corridors.schemas import AreaRef
from gize_api.route_requests import repository
from gize_api.route_requests.models import (
    OPEN_STATUSES,
    ROUTE_REQUEST_AUDIT,
    ROUTE_REQUEST_OPEN_UNIQUE,
    RequestStatus,
    RiderType,
    RouteRequest,
)
from gize_api.route_requests.repository import Asked
from gize_api.route_requests.schemas import (
    AreaPairDemand,
    RouteDemand,
    RouteRef,
    RouteRequestCreate,
    RouteRequestListParams,
    RouteRequestPage,
    RouteRequestRead,
    RouteRequestStatusSet,
    RouteRequestSummary,
    RouteRequestUpdate,
    preferred_times_in_order,
)
from gize_api.routes.models import Route, RouteStatus, RouteType
from gize_api.routes.repository import refer_to_live_route, seat_counts

SUMMARY_LIMIT = 100
NEW, MATCHED, FULFILLED, DECLINED = (
    RequestStatus.NEW,
    RequestStatus.MATCHED,
    RequestStatus.FULFILLED,
    RequestStatus.DECLINED,
)
# Fulfilled and declined are final; a matched request may be matched again.
_MOVES = {
    (NEW, MATCHED), (MATCHED, MATCHED), (NEW, FULFILLED), (MATCHED, FULFILLED),
    (NEW, DECLINED), (MATCHED, DECLINED),
}  # fmt: skip
_RUNNING = (RouteStatus.ACTIVE, RouteStatus.PAUSED)


async def create(
    session: AsyncSession,
    caller: CurrentStaff,
    request: RouteRequestCreate,
    idempotent: IdempotentRequest,
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        # What it names first: one that doesn't exist or fit is a 422, whatever else.
        from_area = await _area_to_refer_to(session, request.from_area_id, "from_area_id")
        to_area = await _area_to_refer_to(session, request.to_area_id, "to_area_id")
        route = None
        if request.route_id is not None:
            route = await _route_to_refer_to(session, request.route_id, "route_id")
            if route.status not in _RUNNING or not _fits(
                route, request.rider_type, request.school_name
            ):
                raise invalid_field("body", "route_id")
        if await repository.open_request_exists(session, _asked(request), except_id=None):
            raise AppError(ErrorCode.ROUTE_REQUEST_EXISTS)
        row = RouteRequest(
            kind=request.kind,
            from_area=from_area,
            from_area_name=request.from_area_name,
            to_area=to_area,
            to_area_name=request.to_area_name,
            route=route,
            rider_type=request.rider_type,
            rider_name=request.rider_name,
            school_name=request.school_name,
            preferred_morning_time=request.preferred_morning_time,
            preferred_return_time=request.preferred_return_time,
            contact_name=request.contact_name,
            contact_phone=request.contact_phone,
            notes=request.notes or None,
            matched_route=None,
        )
        session.add(row)
        await _flush(session)
        await session.refresh(row, ["source", "status", "created_at", "updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route_request.created",
            entity=ROUTE_REQUEST_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(ROUTE_REQUEST_AUDIT, row),
        )
        payload = RouteRequestRead.model_validate(row).model_dump(mode="json")
        location = f"/v1/route-requests/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_requests(session: AsyncSession, params: RouteRequestListParams) -> RouteRequestPage:
    """Oldest first: the order requests are served in."""
    async with session.begin():
        page = await paginate(
            session,
            repository.live_requests(
                status=params.status,
                kind=params.kind,
                open_only=params.open,
                route_id=params.route_id,
            ),
            params,
            created_at=RouteRequest.created_at,
            id=RouteRequest.id,
        )
        return RouteRequestPage(
            items=[RouteRequestRead.model_validate(row) for row in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, request_id: uuid.UUID) -> RouteRequestRead:
    async with session.begin():
        return RouteRequestRead.model_validate(await _live(session, request_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, request_id: uuid.UUID, change: RouteRequestUpdate
) -> RouteRequestRead:
    """Setting what is already there changes and records nothing."""
    async with session.begin():
        row = await _live(session, request_id, for_update=True)
        if "school_name" in change.model_fields_set:
            school = change.school_name
            # Still the school of the route it asks for, and of the one it's matched to.
            names = [row.route, row.matched_route if row.status in OPEN_STATUSES else None]
            not_for_it = row.rider_type is not RiderType.STUDENT or any(
                route is not None and not _fits(route, row.rider_type, school) for route in names
            )
            if not_for_it:
                raise invalid_field("body", "school_name")
        morning = _new(change.preferred_morning_time, row.preferred_morning_time)
        back = _new(change.preferred_return_time, row.preferred_return_time)
        try:
            preferred_times_in_order(morning, back)
        except ValueError:
            sent = change.model_fields_set
            field = (
                "preferred_return_time"
                if "preferred_return_time" in sent
                else "preferred_morning_time"
            )
            raise invalid_field("body", field) from None
        before = audit.snapshot(ROUTE_REQUEST_AUDIT, row)
        for field in ("rider_name", "school_name", "contact_name", "contact_phone"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        row.preferred_morning_time, row.preferred_return_time = morning, back
        if "notes" in change.model_fields_set:
            row.notes = change.notes or None  # empty clears them
        if row.status in OPEN_STATUSES:
            with session.no_autoflush:  # the check, not the index, answers first
                taken = await repository.open_request_exists(session, _asked(row), except_id=row.id)
            if taken:
                raise AppError(ErrorCode.ROUTE_REQUEST_EXISTS)
        await _flush(session)
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route_request.updated",
            entity=ROUTE_REQUEST_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_REQUEST_AUDIT, row),
        )
        return RouteRequestRead.model_validate(row)


async def set_status(
    session: AsyncSession,
    caller: CurrentStaff,
    request_id: uuid.UUID,
    change: RouteRequestStatusSet,
) -> RouteRequestRead:
    """Move the request; the status it has (matched: to the same route) changes nothing.
    The route it names is judged first (a 422), then the move (a 409).

    Lock order: the route, then the request (architecture.md): whatever holds a route's
    lock may go on to lock its requests (GZ-040, freed seats)."""
    async with session.begin():
        await _live(session, request_id)  # unknown or deleted: a 404 before anything else
        route = None
        if change.matched_route_id is not None:
            route = await _route_to_refer_to(session, change.matched_route_id, "matched_route_id")
        row = await _live(session, request_id, for_update=True)
        if route is not None and not _fits(route, row.rider_type, row.school_name):
            raise invalid_field("body", "matched_route_id")
        same_route = change.matched_route_id in (None, row.matched_route_id)
        if change.status is row.status and same_route:
            return RouteRequestRead.model_validate(row)
        if (row.status, change.status) not in _MOVES:
            raise AppError(ErrorCode.INVALID_STATUS_CHANGE)
        before = audit.snapshot(ROUTE_REQUEST_AUDIT, row)
        if route is not None:
            row.matched_route = route
        row.status = change.status
        row.decline_reason = change.decline_reason
        await session.flush()
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route_request.status_changed",
            entity=ROUTE_REQUEST_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_REQUEST_AUDIT, row),
        )
        return RouteRequestRead.model_validate(row)


async def delete(session: AsyncSession, caller: CurrentStaff, request_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role): a mistaken record. History keeps it."""
    async with session.begin():
        row = await _live(session, request_id, for_update=True)
        before = audit.snapshot(ROUTE_REQUEST_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="route_request.deleted",
            entity=ROUTE_REQUEST_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(ROUTE_REQUEST_AUDIT, row),
        )


async def summary(session: AsyncSession) -> RouteRequestSummary:
    """Where the demand is (six queries, however much there is)."""
    async with session.begin():
        pairs = await repository.area_pair_demand(session, SUMMARY_LIMIT + 1)
        targets = await repository.route_demand(session, SUMMARY_LIMIT + 1)
        pairs_truncated, routes_truncated = (
            len(pairs) > SUMMARY_LIMIT,
            len(targets) > SUMMARY_LIMIT,
        )
        pairs, targets = pairs[:SUMMARY_LIMIT], targets[:SUMMARY_LIMIT]
        area_ids = {a for p in pairs for a in (p.from_area_id, p.to_area_id) if a is not None}
        areas = await repository.areas_by_id(session, area_ids)
        route_ids = [route_id for route_id, _ in targets]
        routes = await repository.routes_by_id(session, route_ids)
        counts = await seat_counts(session, route_ids, schedule.addis_today())

        def area(area_id: uuid.UUID | None) -> AreaRef | None:
            return None if area_id is None else AreaRef.model_validate(areas[area_id])

        return RouteRequestSummary(
            area_pairs=[
                AreaPairDemand(
                    from_area=area(pair.from_area_id),
                    from_area_name=pair.from_area_name,
                    to_area=area(pair.to_area_id),
                    to_area_name=pair.to_area_name,
                    open=pair.open,
                )
                for pair in pairs
            ],
            area_pairs_truncated=pairs_truncated,
            routes=[
                RouteDemand(
                    route=RouteRef.model_validate(routes[route_id]),
                    status=routes[route_id].status,
                    open=count,
                    seats_free_next_month=max(
                        routes[route_id].seats - counts[route_id].next_month, 0
                    ),
                )
                for route_id, count in targets
            ],
            routes_truncated=routes_truncated,
        )


def _asked(request: RouteRequestCreate | RouteRequest) -> Asked:
    return Asked(
        contact_phone=request.contact_phone,
        kind=request.kind,
        route_id=request.route_id,
        from_area_id=request.from_area_id,
        from_area_name=request.from_area_name,
        to_area_id=request.to_area_id,
        to_area_name=request.to_area_name,
        rider_name=request.rider_name,
    )


def _fits(route: Route, rider_type: RiderType, school_name: str | None) -> bool:
    """A route for this rider: public for adults; student, of their school (ignoring case),
    for students. Corporate routes take company riders only."""
    if rider_type is RiderType.ADULT:
        return route.route_type is RouteType.PUBLIC
    return (
        route.route_type is RouteType.STUDENT
        and route.school_name is not None
        and school_name is not None
        and route.school_name.casefold() == school_name.casefold()
    )


def _new[T](change: T | None, current: T) -> T:
    return current if change is None else change


async def _live(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> RouteRequest:
    row = await repository.get_live_request(session, request_id, for_update=for_update)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _area_to_refer_to(
    session: AsyncSession, area_id: uuid.UUID | None, field: str
) -> Area | None:
    if area_id is None:
        return None
    area = await get_live_area(session, area_id, lock="share")
    if area is None:
        raise invalid_field("body", field)
    return area


async def _route_to_refer_to(session: AsyncSession, route_id: uuid.UUID, field: str) -> Route:
    route = await refer_to_live_route(session, route_id)
    if route is None:
        raise invalid_field("body", field)
    return route


async def _flush(session: AsyncSession) -> None:
    """Flush; the same open request another call recorded meanwhile is the check's 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _exists_or_raise(exc)


def _exists_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == ROUTE_REQUEST_OPEN_UNIQUE:
        raise AppError(ErrorCode.ROUTE_REQUEST_EXISTS) from None
    raise exc
