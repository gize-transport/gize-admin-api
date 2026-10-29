"""Staff queries."""

import uuid

from sqlalchemy import Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.staff.models import Staff, StaffRole

# Every change to staff (an invite, a role or status change, the first-admin command) runs
# under this transaction-level lock, so checks such as "another active admin remains" see
# nothing change before the write commits. Staff changes are rare: one at a time is fine.
STAFF_CHANGES_LOCK_ID = 0x67697A6561646D  # "gizeadm"


async def lock_staff_changes(session: AsyncSession) -> None:
    """Wait (up to the role's lock_timeout, then 503) for other staff changes to commit."""
    await session.execute(select(func.pg_advisory_xact_lock(STAFF_CHANGES_LOCK_ID)))


async def get_staff(session: AsyncSession, staff_id: uuid.UUID) -> Staff | None:
    return await session.get(Staff, staff_id)


async def get_staff_by_email(session: AsyncSession, email: str) -> Staff | None:
    return await session.scalar(select(Staff).where(Staff.email == email))


async def admin_exists(session: AsyncSession) -> bool:
    """Any admin, active or not."""
    return bool(await session.scalar(select(exists().where(Staff.role == StaffRole.ADMIN))))


async def other_active_admin_exists(session: AsyncSession, staff_id: uuid.UUID) -> bool:
    """An active admin other than `staff_id`."""
    others = exists().where(Staff.role == StaffRole.ADMIN, Staff.is_active, Staff.id != staff_id)
    return bool(await session.scalar(select(others)))


def staff_members(*, role: StaffRole | None, is_active: bool | None) -> Select[Staff]:
    """The filtered members, unordered: `paginate` orders and bounds them."""
    statement = select(Staff)
    if role is not None:
        statement = statement.where(Staff.role == role)
    if is_active is not None:
        statement = statement.where(Staff.is_active.is_(is_active))
    return statement
