"""GZ-011: staff services re-check the caller inside their own transaction."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.security import CurrentStaff
from gize_api.staff import service
from gize_api.staff.models import StaffRole
from tests.factories import StaffFactory


async def test_me_for_staff_deactivated_after_sign_in_is_forbidden(
    db_session: AsyncSession,
) -> None:
    """`current_staff` read the row moments before; the service reads it again."""
    async with db_session.begin():
        staff = await StaffFactory().create(db_session)
    caller = CurrentStaff(id=staff.id, role=StaffRole.STAFF)
    async with db_session.begin():
        staff.is_active = False
    with pytest.raises(AppError) as exc_info:
        await service.get_me(db_session, caller)
    assert exc_info.value.code is ErrorCode.FORBIDDEN


async def test_me_for_a_removed_staff_member_is_forbidden(db_session: AsyncSession) -> None:
    caller = CurrentStaff(id=uuid.uuid4(), role=StaffRole.ADMIN)
    with pytest.raises(AppError) as exc_info:
        await service.get_me(db_session, caller)
    assert exc_info.value.code is ErrorCode.FORBIDDEN
