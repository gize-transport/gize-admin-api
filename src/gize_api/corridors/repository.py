"""Corridor queries: live (not deleted) corridors, with both areas in the same query."""

import uuid

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from gize_api.corridors.models import Corridor

# Both ends are required, so inner joins: one query, and FOR UPDATE allowed.
_WITH_AREAS = (
    joinedload(Corridor.origin_area, innerjoin=True),
    joinedload(Corridor.destination_area, innerjoin=True),
)


async def get_live_corridor(
    session: AsyncSession, corridor_id: uuid.UUID, *, for_update: bool = False
) -> Corridor | None:
    """The corridor with its areas. `for_update` first locks the corridor row alone, then
    loads it: after waiting, Postgres re-checks a locking query against the corridor's new
    version but joins the area rows it read before the wait, so a locking query with the
    joins would lose a corridor whose end just changed. The load is a new statement, so it
    sees the committed change."""
    live = (Corridor.id == corridor_id, Corridor.not_deleted())
    if for_update:
        locked = await session.scalar(select(Corridor.id).where(*live).with_for_update())
        if locked is None:
            return None
    return await session.scalar(select(Corridor).options(*_WITH_AREAS).where(*live))


async def pair_taken(
    session: AsyncSession,
    origin_area_id: uuid.UUID,
    destination_area_id: uuid.UUID,
    *,
    except_id: uuid.UUID | None,
) -> bool:
    statement = exists().where(
        Corridor.origin_area_id == origin_area_id,
        Corridor.destination_area_id == destination_area_id,
        Corridor.not_deleted(),
    )
    if except_id is not None:
        statement = statement.where(Corridor.id != except_id)
    return bool(await session.scalar(select(statement)))


async def uses_area(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live corridor starts or ends in this area (indexes: the pair's, and destination)."""
    statement = exists().where(
        or_(Corridor.origin_area_id == area_id, Corridor.destination_area_id == area_id),
        Corridor.not_deleted(),
    )
    return bool(await session.scalar(select(statement)))


def live_corridors(
    *, origin_area_id: uuid.UUID | None, destination_area_id: uuid.UUID | None
) -> Select[Corridor]:
    """The filtered corridors with their areas, unordered: `paginate` orders and bounds."""
    statement = select(Corridor).options(*_WITH_AREAS).where(Corridor.not_deleted())
    if origin_area_id is not None:
        statement = statement.where(Corridor.origin_area_id == origin_area_id)
    if destination_area_id is not None:
        statement = statement.where(Corridor.destination_area_id == destination_area_id)
    return statement
