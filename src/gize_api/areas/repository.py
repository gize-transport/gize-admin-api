"""Area queries: live (not deleted) areas only. Names are compared with the database's
`lower()`, the function the unique index uses, so a check and the index always agree."""

import uuid
from typing import Literal

from sqlalchemy import Select, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.core.db import LIKE_ESCAPE, escape_like

Lock = Literal["update", "share"]

# Adding or renaming an area, and recording a request with a typed area name, run under
# this transaction-level lock, taken before any row lock (GZ-077): a request typed while
# its area is added either finds the area or is linked by the add. Both are rare.
AREA_NAMES_LOCK_ID = 0x67697A6561726561  # "gizearea"


async def lock_area_names(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(AREA_NAMES_LOCK_ID)))


async def get_live_area(
    session: AsyncSession, area_id: uuid.UUID, *, lock: Lock | None = None
) -> Area | None:
    """`lock` holds the row until the transaction ends; Postgres re-checks "not deleted" on
    the newest version after waiting, so the loser of a race finds the area gone.

    - "update": to rename or delete it (one at a time). FOR NO KEY UPDATE: neither changes
      the key, so an insert that only refers to the area (a foreign key check, such as a
      new rider's when a seat offer is accepted) doesn't wait. A rename then locks
      requests (GZ-077), so such an insert holding a request must not wait on the area;
    - "share": to refer to it (a new corridor): many may share, but a delete waits for them,
      and they wait for a delete.
    """
    statement = select(Area).where(Area.id == area_id, Area.not_deleted())
    if lock == "update":
        statement = statement.with_for_update(key_share=True)
    elif lock == "share":
        statement = statement.with_for_update(read=True)
    return await session.scalar(statement)


async def name_taken(session: AsyncSession, name: str, *, except_id: uuid.UUID | None) -> bool:
    """A live area other than `except_id` has this name, ignoring case."""
    statement = exists().where(
        func.lower(Area.name) == func.lower(literal(name)), Area.not_deleted()
    )
    if except_id is not None:
        statement = statement.where(Area.id != except_id)
    return bool(await session.scalar(select(statement)))


async def live_area_named(session: AsyncSession, name: str) -> Area | None:
    """The live area with this name, ignoring case, share-locked (a request refers to it)."""
    statement = (
        select(Area)
        .where(func.lower(Area.name) == func.lower(literal(name)), Area.not_deleted())
        .with_for_update(read=True)
    )
    return await session.scalar(statement)


def live_areas(*, q: str | None) -> Select[Area]:
    """The filtered areas, unordered: `paginate` orders and bounds them."""
    statement = select(Area).where(Area.not_deleted())
    if q is not None:
        statement = statement.where(
            func.lower(Area.name).startswith(
                func.lower(literal(escape_like(q))), escape=LIKE_ESCAPE
            )
        )
    return statement
