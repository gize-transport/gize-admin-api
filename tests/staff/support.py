"""Staff-management test helpers (GZ-013): a fake Supabase invite endpoint and shortcuts."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.audit import AuditEntry
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.tokens import bearer, make_token

STAFF = "/v1/staff"


async def add_staff(session: AsyncSession, **values: Any) -> Staff:  # noqa: ANN401
    """A committed (or, in `db_session`, rolled-back-later) staff row."""
    async with session.begin():
        return await StaffFactory().create(session, **values)


SIGNED_IN_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


async def add_admin(session: AsyncSession, **values: Any) -> Staff:  # noqa: ANN401
    """An admin who has signed in before (pass `first_signed_in_at=None` for one who has
    not): only those count as the active admin who must remain."""
    values.setdefault("first_signed_in_at", SIGNED_IN_AT)
    return await add_staff(session, role=StaffRole.ADMIN, **values)


def signed_in(staff_id: uuid.UUID) -> dict[str, str]:
    return bearer(make_token(sub=str(staff_id)))


def with_key(staff_id: uuid.UUID, key: uuid.UUID | None = None) -> dict[str, str]:
    """Signed in, with an Idempotency-Key (a new one unless given)."""
    return {**signed_in(staff_id), "Idempotency-Key": str(key or uuid.uuid4())}


async def audit_entries(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditEntry]:
    async with session.begin():
        statement = (
            select(AuditEntry)
            .where(AuditEntry.entity_id == entity_id)
            .order_by(AuditEntry.created_at, AuditEntry.action)
        )
        return list((await session.scalars(statement)).all())


async def active_admins(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session, session.begin():
        statement = select(func.count()).where(Staff.role == StaffRole.ADMIN, Staff.is_active)
        return int(await session.scalar(statement.select_from(Staff)) or 0)
