"""Area queries: live (not deleted) areas only."""

import uuid

from sqlalchemy import Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area


async def get_live_area(session: AsyncSession, area_id: uuid.UUID) -> Area | None:
    return await session.scalar(select(Area).where(Area.id == area_id, Area.not_deleted()))


async def name_taken(session: AsyncSession, name: str, *, except_id: uuid.UUID | None) -> bool:
    """A live area other than `except_id` has this name, ignoring case."""
    statement = exists().where(func.lower(Area.name) == name.lower(), Area.not_deleted())
    if except_id is not None:
        statement = statement.where(Area.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_areas(*, q: str | None) -> Select[Area]:
    """The filtered areas, unordered: `paginate` orders and bounds them."""
    statement = select(Area).where(Area.not_deleted())
    if q is not None:
        # LIKE wildcards in `q` are escaped: "%" matches a percent sign.
        statement = statement.where(func.lower(Area.name).startswith(q.lower(), autoescape=True))
    return statement
