"""Corridor queries: live (not deleted) corridors, with both areas in the same query."""

import uuid
from typing import Literal

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, contains_eager, joinedload

from gize_api.areas.models import Area
from gize_api.core.pagination import SortKey
from gize_api.corridors.models import Corridor

Lock = Literal["update", "share"]

# Both ends are required, so inner joins: one query, and FOR UPDATE allowed.
_WITH_AREAS = (
    joinedload(Corridor.origin_area, innerjoin=True),
    joinedload(Corridor.destination_area, innerjoin=True),
)


async def get_live_corridor(
    session: AsyncSession, corridor_id: uuid.UUID, *, lock: Lock | None = None
) -> Corridor | None:
    """The corridor with its areas. `lock` first locks the corridor row alone, then loads
    it: after waiting, Postgres re-checks a locking query against the corridor's new version
    but joins the area rows it read before the wait, so a locking query with the joins would
    lose a corridor whose end just changed. The load is a new statement, so it sees the
    committed change.

    - "update": to change or delete it (one at a time);
    - "share": to refer to it (a route): many may share; a change or delete waits for them,
      and they for it.
    """
    live = (Corridor.id == corridor_id, Corridor.not_deleted())
    if lock is not None:
        statement = select(Corridor.id).where(*live).with_for_update(read=lock == "share")
        if await session.scalar(statement) is None:
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


# A corridor's name (GZ-082): its A area's, then its B area's. The list is small (a pair
# of places per corridor), so it sorts without an index.
_ORIGIN = aliased(Area)
_DESTINATION = aliased(Area)
BY_NAME = SortKey(
    columns=(_ORIGIN.name, _DESTINATION.name),
    read=lambda corridor: (corridor.origin_area.name, corridor.destination_area.name),
)


def live_corridors(
    *, origin_area_id: uuid.UUID | None, destination_area_id: uuid.UUID | None, by_name: bool
) -> Select[Corridor]:
    """The filtered corridors with their areas, unordered: `paginate` orders and bounds.
    `by_name` joins the areas `BY_NAME` sorts on and loads them from those joins."""
    statement = select(Corridor).where(Corridor.not_deleted())
    if by_name:
        statement = (
            statement.join(_ORIGIN, _ORIGIN.id == Corridor.origin_area_id)
            .join(_DESTINATION, _DESTINATION.id == Corridor.destination_area_id)
            .options(
                contains_eager(Corridor.origin_area.of_type(_ORIGIN)),
                contains_eager(Corridor.destination_area.of_type(_DESTINATION)),
            )
        )
    else:
        statement = statement.options(*_WITH_AREAS)
    if origin_area_id is not None:
        statement = statement.where(Corridor.origin_area_id == origin_area_id)
    if destination_area_id is not None:
        statement = statement.where(Corridor.destination_area_id == destination_area_id)
    return statement
