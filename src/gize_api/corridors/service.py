"""Corridor business rules (GZ-020). Any active staff member manages corridors; only admins
delete them, and deleting is soft. Every change is audited.

A corridor refers to two live areas. It takes a share lock on each while it is created or
changed, and deleting an area takes an update lock, so the two never cross: a corridor
never points at a deleted area, and an area in use is never deleted.
"""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.areas.repository import get_live_area
from gize_api.core import audit
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode, invalid_field
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.corridors import repository
from gize_api.corridors.models import CORRIDOR_AREAS_UNIQUE, CORRIDOR_AUDIT, Corridor
from gize_api.corridors.schemas import (
    CorridorCreate,
    CorridorListParams,
    CorridorPage,
    CorridorRead,
    CorridorUpdate,
)
from gize_api.routes.repository import uses_corridor as route_uses_corridor


async def create(
    session: AsyncSession,
    caller: CurrentStaff,
    corridor: CorridorCreate,
    idempotent: IdempotentRequest,
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        origin = await _area_to_refer_to(session, corridor.origin_area_id, "origin_area_id")
        destination = await _area_to_refer_to(
            session, corridor.destination_area_id, "destination_area_id"
        )
        if await repository.pair_taken(session, origin.id, destination.id, except_id=None):
            raise AppError(ErrorCode.CORRIDOR_EXISTS)
        row = Corridor(origin_area=origin, destination_area=destination)
        session.add(row)
        await _flush(session)
        audit.record(
            session,
            actor_id=caller.id,
            action="corridor.created",
            entity=CORRIDOR_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(CORRIDOR_AUDIT, row),
        )
        payload = CorridorRead.model_validate(row).model_dump(mode="json")
        location = f"/v1/corridors/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_corridors(session: AsyncSession, params: CorridorListParams) -> CorridorPage:
    """Newest first or by name (A area, then B), with both areas' names."""
    async with session.begin():
        page = await paginate(
            session,
            repository.live_corridors(
                origin_area_id=params.origin_area_id,
                destination_area_id=params.destination_area_id,
                by_name=params.order is not None,
            ),
            params,
            created_at=Corridor.created_at,
            id=Corridor.id,
            newest_first=True,
            sort_keys={"name": repository.BY_NAME},
        )
        return CorridorPage(
            items=[CorridorRead.model_validate(corridor) for corridor in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, corridor_id: uuid.UUID) -> CorridorRead:
    async with session.begin():
        return CorridorRead.model_validate(await _live(session, corridor_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, corridor_id: uuid.UUID, change: CorridorUpdate
) -> CorridorRead:
    """Changing an end to what it is changes and records nothing. While a live route runs
    on the corridor its ends can't change (owner decision): routes move to another
    corridor instead, or, once priced or requested, a route is added on the new one."""
    async with session.begin():
        row = await _live(session, corridor_id, lock="update")
        origin, destination = row.origin_area, row.destination_area
        if change.origin_area_id not in (None, origin.id):
            origin = await _area_to_refer_to(session, change.origin_area_id, "origin_area_id")
        if change.destination_area_id not in (None, destination.id):
            destination = await _area_to_refer_to(
                session, change.destination_area_id, "destination_area_id"
            )
        if origin.id == destination.id:
            raise invalid_field("body")
        moved = (origin.id, destination.id) != (row.origin_area_id, row.destination_area_id)
        if moved and await route_uses_corridor(session, row.id):
            raise AppError(ErrorCode.CORRIDOR_IN_USE)  # routes never change meaning underneath
        if await repository.pair_taken(session, origin.id, destination.id, except_id=row.id):
            raise AppError(ErrorCode.CORRIDOR_EXISTS)
        before = audit.snapshot(CORRIDOR_AUDIT, row)
        row.origin_area, row.destination_area = origin, destination
        await _flush(session)
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="corridor.updated",
            entity=CORRIDOR_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(CORRIDOR_AUDIT, row),
        )
        return CorridorRead.model_validate(row)


async def delete(session: AsyncSession, caller: CurrentStaff, corridor_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role), refused while a live route runs on
    the corridor (`CORRIDOR_IN_USE`)."""
    async with session.begin():
        row = await _live(session, corridor_id, lock="update")
        if await route_uses_corridor(session, row.id):
            raise AppError(ErrorCode.CORRIDOR_IN_USE)
        before = audit.snapshot(CORRIDOR_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="corridor.deleted",
            entity=CORRIDOR_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(CORRIDOR_AUDIT, row),
        )


async def _live(
    session: AsyncSession, corridor_id: uuid.UUID, *, lock: repository.Lock | None = None
) -> Corridor:
    row = await repository.get_live_corridor(session, corridor_id, lock=lock)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _area_to_refer_to(session: AsyncSession, area_id: uuid.UUID, field: str) -> Area:
    """The live area, share-locked until the transaction ends; 422 on `field` otherwise."""
    area = await get_live_area(session, area_id, lock="share")
    if area is None:
        raise invalid_field("body", field)
    return area


async def _flush(session: AsyncSession) -> None:
    """Flush; a pair another request took meanwhile is the same 409 as the check's."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _pair_taken_or_raise(exc)


def _pair_taken_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == CORRIDOR_AREAS_UNIQUE:
        raise AppError(ErrorCode.CORRIDOR_EXISTS) from None
    raise exc
