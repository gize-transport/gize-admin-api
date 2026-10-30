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
    """`for_update` locks the corridor (not its areas) until the transaction ends."""
    statement = (
        select(Corridor)
        .options(*_WITH_AREAS)
        .where(Corridor.id == corridor_id, Corridor.not_deleted())
    )
    if for_update:
        statement = statement.with_for_update(of=Corridor)
    return await session.scalar(statement)


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
