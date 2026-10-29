"""GZ-011: the staff table keeps its rules even against a buggy writer."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory


async def test_a_staff_member_with_defaults(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    async with db_session.begin():
        staff = await StaffFactory().create(db_session, id=user_id, role=StaffRole.ADMIN)
    async with db_session.begin():
        row = (await db_session.execute(select(Staff).where(Staff.id == user_id))).scalar_one()
    assert row is staff
    assert row.is_active is True
    assert row.role is StaffRole.ADMIN
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "Someone@Example.com"},  # stored lower-case only
        {"full_name": "   "},
        {"email": " "},
        {"email": " a@example.com"},  # stored trimmed
        {"email": "no-at-sign"},
        {"email": "@example.com"},
    ],
)
async def test_invalid_rows_are_refused_by_the_database(
    db_session: AsyncSession, overrides: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin():
            await StaffFactory().create(db_session, **overrides)


async def test_an_email_belongs_to_one_staff_member(db_session: AsyncSession) -> None:
    async with db_session.begin():
        await StaffFactory().create(db_session, email="same@example.com")
    with pytest.raises(IntegrityError):
        async with db_session.begin():
            await StaffFactory().create(db_session, email="same@example.com")


async def test_an_unknown_role_is_refused_by_the_code_and_the_database(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(StatementError):
        async with db_session.begin():
            await StaffFactory().create(db_session, role="owner")
    untyped = sa.table(  # what a writer without our model would send
        "staff",
        sa.column("id", sa.Uuid()),
        sa.column("full_name", sa.String()),
        sa.column("email", sa.String()),
        sa.column("role", sa.String()),
        schema="gize",
    )
    row = {"id": uuid.uuid4(), "full_name": "A", "email": "a@example.com", "role": "owner"}
    with pytest.raises(IntegrityError):
        async with db_session.begin():
            await db_session.execute(sa.insert(untyped).values(**row))
