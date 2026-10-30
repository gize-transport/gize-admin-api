"""Area business rules (GZ-026). Any active staff member manages areas; only admins delete
them, and deleting is soft. Every change is audited."""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas import repository
from gize_api.areas.models import AREA_AUDIT, AREA_NAME_UNIQUE, Area
from gize_api.areas.schemas import AreaListParams, AreaPage, AreaRead, AreaWrite
from gize_api.core import audit
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.corridors.repository import uses_area as corridor_uses_area


async def create(
    session: AsyncSession, caller: CurrentStaff, area: AreaWrite, idempotent: IdempotentRequest
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        if await repository.name_taken(session, area.name, except_id=None):
            raise AppError(ErrorCode.AREA_NAME_TAKEN)
        row = Area(name=area.name)
        session.add(row)
        await _flush(session)
        audit.record(
            session,
            actor_id=caller.id,
            action="area.created",
            entity=AREA_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(AREA_AUDIT, row),
        )
        payload = AreaRead.model_validate(row).model_dump(mode="json")
        location = f"/v1/areas/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_areas(session: AsyncSession, params: AreaListParams) -> AreaPage:
    """Newest first."""
    async with session.begin():
        page = await paginate(
            session,
            repository.live_areas(q=params.q),
            params,
            created_at=Area.created_at,
            id=Area.id,
            newest_first=True,
        )
        return AreaPage(
            items=[AreaRead.model_validate(area) for area in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, area_id: uuid.UUID) -> AreaRead:
    async with session.begin():
        return AreaRead.model_validate(await _live(session, area_id))


async def rename(
    session: AsyncSession, caller: CurrentStaff, area_id: uuid.UUID, change: AreaWrite
) -> AreaRead:
    """Renaming to the current name changes and records nothing."""
    async with session.begin():
        row = await _live(session, area_id, lock="update")
        if await repository.name_taken(session, change.name, except_id=row.id):
            raise AppError(ErrorCode.AREA_NAME_TAKEN)
        before = audit.snapshot(AREA_AUDIT, row)
        row.name = change.name
        await _flush(session)
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="area.renamed",
            entity=AREA_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(AREA_AUDIT, row),
        )
        return AreaRead.model_validate(row)


async def delete(session: AsyncSession, caller: CurrentStaff, area_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role), refused while the area is in use
    (`_in_use`). The update lock waits for anything referring to the area right now."""
    async with session.begin():
        row = await _live(session, area_id, lock="update")
        if await _in_use(session, row.id):
            raise AppError(ErrorCode.AREA_IN_USE)
        before = audit.snapshot(AREA_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="area.deleted",
            entity=AREA_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(AREA_AUDIT, row),
        )


async def _in_use(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """Live corridors use it (GZ-020). Pickup points (GZ-023), requests (GZ-027) and riders
    (M3) add their checks here."""
    return await corridor_uses_area(session, area_id)


async def _live(
    session: AsyncSession, area_id: uuid.UUID, *, lock: repository.Lock | None = None
) -> Area:
    row = await repository.get_live_area(session, area_id, lock=lock)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _flush(session: AsyncSession) -> None:
    """Flush; a name another request took meanwhile is the same 409 as the check's."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _name_taken_or_raise(exc)


def _name_taken_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == AREA_NAME_UNIQUE:
        raise AppError(ErrorCode.AREA_NAME_TAKEN) from None
    raise exc
