"""Driver queries: live (not deleted) drivers only."""

import uuid
from datetime import time

from sqlalchemy import Select, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import LIKE_ESCAPE, escape_like
from gize_api.drivers.models import Driver
from gize_api.routes.models import Route


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


Times = tuple[time, time | None, time, time | None]


async def drives_a_route(session: AsyncSession, driver_id: uuid.UUID) -> bool:
    """A live route has this driver (index: ix_routes_driver_id)."""
    statement = exists().where(Route.driver_id == driver_id, Route.not_deleted())
    return bool(await session.scalar(select(statement)))


async def busy(
    session: AsyncSession, driver_id: uuid.UUID, times: Times, *, excluding: uuid.UUID
) -> bool:
    """The driver's other live routes have a run overlapping one of these (GZ-053): the
    morning run (leaves A to arrives B) or the return run (leaves B to arrives A). Runs
    that only touch (one arrives as the other leaves) don't overlap."""
    rows = await session.execute(
        select(
            Route.morning_time,
            Route.morning_arrival_time,
            Route.return_time,
            Route.return_arrival_time,
        ).where(Route.driver_id == driver_id, Route.not_deleted(), Route.id != excluding)
    )
    mine = _runs(times)
    return any(
        start < their_end and their_start < end
        for other in rows
        for their_start, their_end in _runs(tuple(other))  # type: ignore[arg-type]
        for start, end in mine
    )


def _runs(times: Times) -> tuple[tuple[time, time], tuple[time, time]]:
    """A route made before arrival times existed (null) runs from its departure only."""
    leaves_a, arrives_b, leaves_b, arrives_a = times
    return (leaves_a, arrives_b or leaves_a), (leaves_b, arrives_a or leaves_b)
