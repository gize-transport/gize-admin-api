"""Staff queries."""

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.staff.models import Staff, StaffRole


async def get_staff(session: AsyncSession, staff_id: uuid.UUID) -> Staff | None:
    return await session.get(Staff, staff_id)


async def get_staff_by_email(session: AsyncSession, email: str) -> Staff | None:
    return await session.scalar(select(Staff).where(Staff.email == email))


async def admin_exists(session: AsyncSession) -> bool:
    """Any admin, active or not."""
    return bool(await session.scalar(select(exists().where(Staff.role == StaffRole.ADMIN))))
