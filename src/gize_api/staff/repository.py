"""Staff queries."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.staff.models import Staff


async def get_staff(session: AsyncSession, staff_id: uuid.UUID) -> Staff | None:
    return await session.get(Staff, staff_id)
