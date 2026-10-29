"""Staff business rules."""

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.security import CurrentStaff
from gize_api.staff.repository import get_staff
from gize_api.staff.schemas import StaffMe


async def get_me(session: AsyncSession, caller: CurrentStaff) -> StaffMe:
    async with session.begin():
        staff = await get_staff(session, caller.id)
        # Deactivated (or removed) since `current_staff` read it moments ago.
        if staff is None or not staff.is_active:
            raise AppError(ErrorCode.FORBIDDEN)
        return StaffMe(id=staff.id, full_name=staff.full_name, email=staff.email, role=staff.role)
