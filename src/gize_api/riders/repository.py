"""Rider queries."""

import uuid

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.riders.models import Rider


async def lives_or_goes_to(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live rider's home or destination is this area (indexes on both columns)."""
    statement = exists().where(
        or_(Rider.home_area_id == area_id, Rider.destination_area_id == area_id),
        Rider.not_deleted(),
    )
    return bool(await session.scalar(select(statement)))
