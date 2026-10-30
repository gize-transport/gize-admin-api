"""Area queries: live (not deleted) areas only. Names are compared with the database's
`lower()`, the function the unique index uses, so a check and the index always agree."""

import uuid

from sqlalchemy import Select, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area

LIKE_ESCAPE = "/"


async def get_live_area(
    session: AsyncSession, area_id: uuid.UUID, *, for_update: bool = False
) -> Area | None:
    """`for_update` locks the row until the transaction ends: a change racing a delete waits,
    then finds the area deleted (Postgres re-checks the filter on the newest version)."""
    statement = select(Area).where(Area.id == area_id, Area.not_deleted())
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def name_taken(session: AsyncSession, name: str, *, except_id: uuid.UUID | None) -> bool:
    """A live area other than `except_id` has this name, ignoring case."""
    statement = exists().where(
        func.lower(Area.name) == func.lower(literal(name)), Area.not_deleted()
    )
    if except_id is not None:
        statement = statement.where(Area.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_areas(*, q: str | None) -> Select[Area]:
    """The filtered areas, unordered: `paginate` orders and bounds them."""
    statement = select(Area).where(Area.not_deleted())
    if q is not None:
        statement = statement.where(
            func.lower(Area.name).startswith(func.lower(literal(_escaped(q))), escape=LIKE_ESCAPE)
        )
    return statement


def _escaped(text: str) -> str:
    """LIKE wildcards in `text` as plain characters: "%" matches a percent sign."""
    for char in (LIKE_ESCAPE, "%", "_"):
        text = text.replace(char, LIKE_ESCAPE + char)
    return text
