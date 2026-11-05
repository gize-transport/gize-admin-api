"""Driver queries: live (not deleted) drivers only."""

import uuid

from sqlalchemy import Select, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import LIKE_ESCAPE, escape_like
from gize_api.drivers.models import Driver


async def get_live_driver(
    session: AsyncSession, driver_id: uuid.UUID, *, for_update: bool = False
) -> Driver | None:
    statement = select(Driver).where(Driver.id == driver_id, Driver.not_deleted())
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def taken(
    session: AsyncSession, column: str, value: str, *, except_id: uuid.UUID | None
) -> bool:
    """Another live driver has this phone or plate (the unique indexes' rule)."""
    statement = exists().where(getattr(Driver, column) == value, Driver.not_deleted())
    if except_id is not None:
        statement = statement.where(Driver.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_drivers(*, q: str | None, plate_q: str | None, is_active: bool | None) -> Select[Driver]:
    """The filtered drivers, unordered: `paginate` orders and bounds them. `q` matches the
    start of a name (ignoring case), `plate_q` (its plate form) the start of a plate."""
    statement = select(Driver).where(Driver.not_deleted())
    if q is not None and plate_q is not None:
        statement = statement.where(
            or_(
                func.lower(Driver.full_name).startswith(
                    func.lower(literal(escape_like(q))), escape=LIKE_ESCAPE
                ),
                Driver.plate.startswith(literal(escape_like(plate_q)), escape=LIKE_ESCAPE),
            )
        )
    if is_active is not None:
        statement = statement.where(Driver.is_active.is_(is_active))
    return statement
