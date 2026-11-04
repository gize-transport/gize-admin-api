"""Rider business rules (GZ-034). Any active staff member records, lists, looks up, reads
and edits riders; only admins delete them (soft), and never while they hold a seat (end it
first, GZ-035). Every staff change is audited, with names and phones redacted.

A rider holding a seat must still fit its route after an edit: boarding at A or an area it
serves, going to B, the Saturday add-on only where offered. Locks: the rider, then the
route of their seat (architecture.md, "Row locks"), so a route change and a rider edit
never judge each other's old state.
"""

import uuid
from datetime import date
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.repository import get_live_area
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
    SeatRef,
    SeatState,
    masked,
)
from gize_api.routes import repository as routes
from gize_api.routes.models import AssignmentStatus

_AREA_FIELDS = ("home_area_id", "destination_area_id")


async def create(
    session: AsyncSession, caller: CurrentStaff, rider: RiderCreate, idempotent: IdempotentRequest
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        await _refer_to_areas(
            session, {"home_area_id": rider.home_area_id,
                      "destination_area_id": rider.destination_area_id},
        )  # fmt: skip
        if await repository.phone_taken(session, rider.phone, except_id=None):
            raise AppError(ErrorCode.RIDER_EXISTS)
        row = Rider(
            rider_type=RiderType.ADULT,
            full_name=rider.full_name,
            phone=rider.phone,
            home_area_id=rider.home_area_id,
            destination_area_id=rider.destination_area_id,
            plan=rider.plan,
            saturday_addon=rider.saturday_addon,
            source=RiderSource.STAFF,
        )
        session.add(row)
        await _flush(session)
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
        payload = _read(row, None, 0).model_dump(mode="json")
        location = f"/v1/riders/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_riders(session: AsyncSession, params: RiderListParams) -> RiderPage:
    """Newest first, each with their seat and open requests (four queries per page)."""
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
        return await _read_with_seat(session, await _live(session, rider_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, rider_id: uuid.UUID, change: RiderUpdate
) -> RiderRead:
    """Setting what is already there changes and records nothing."""
    async with session.begin():
        row = await _live(session, rider_id, for_update=True)
        today = schedule.addis_today()
        held = (await repository.held_seats(session, [row.id], today)).get(row.id)
        route = (
            None
            if held is None
            else await routes.get_live_route(session, held.route_id, for_update=True)
        )
        home = change.home_area_id or row.home_area_id
        destination = change.destination_area_id or row.destination_area_id
        moved = [field for field in _AREA_FIELDS if field in change.model_fields_set]
        if home == destination:
            raise invalid_field("body", moved[-1])
        await _refer_to_areas(session, {field: getattr(change, field) for field in moved})
        if route is not None:
            if moved and (
                home is None
                or destination is None
                or not await routes.serves(session, route.id, home, destination)
            ):
                raise invalid_field("body", moved[0])
            if change.saturday_addon and route.price_saturday is None:
                raise invalid_field("body", "saturday_addon")
        if change.phone is not None and await repository.phone_taken(
            session, change.phone, except_id=row.id
        ):
            raise AppError(ErrorCode.RIDER_EXISTS)
        before = audit.snapshot(RIDER_AUDIT, row)
        for field in ("full_name", "phone", *moved, "plan", "saturday_addon"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        await _flush(session)
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
        return await _read_with_seat(session, row)


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
    seats = await repository.held_seats(session, ids, today)
    requests = await repository.open_request_counts(session, ids)
    return [
        RiderListItem.model_validate(
            {
                **_read(row, seats.get(row.id), requests.get(row.id, 0)).model_dump(
                    exclude={"phone"}
                ),
                "phone_masked": masked(row.phone),
            }
        )
        for row in rows
    ]


async def _read_with_seat(session: AsyncSession, row: Rider) -> RiderRead:
    seat = (await repository.held_seats(session, [row.id], schedule.addis_today())).get(row.id)
    requests = (await repository.open_request_counts(session, [row.id])).get(row.id, 0)
    return _read(row, seat, requests)


def _read(row: Rider, seat: HeldSeat | None, open_requests: int) -> RiderRead:
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
        seat=None
        if seat is None
        else SeatRef(
            route_id=seat.route_id,
            route_code=seat.route_code,
            status=seat.status,
            start_date=seat.start_date,
            end_date=seat.end_date,
        ),
        open_requests=open_requests,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _flush(session: AsyncSession) -> None:
    """Flush; a phone another request took meanwhile is the same 409 as the check's."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _rider_exists_or_raise(exc)


def _rider_exists_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == RIDER_ADULT_PHONE_UNIQUE:
        raise AppError(ErrorCode.RIDER_EXISTS) from None
    raise exc
