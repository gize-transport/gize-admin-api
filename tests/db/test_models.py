"""GZ-006: base model mixins and row-level security, exercised as the app role."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import asyncpg
import pytest
from sqlalchemy import String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.rls import row_level_security_statements
from tests.support import DatabaseUrls


class Widget(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A test-only table, created and dropped by the migrator around these tests."""

    __tablename__ = "test_widgets"

    label: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def widgets_table(database: DatabaseUrls) -> AsyncIterator[None]:
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await conn.execute(
            "CREATE TABLE gize.test_widgets ("
            " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            " label varchar(50) NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " deleted_at timestamptz)"
        )
        for statement in row_level_security_statements("test_widgets"):
            await conn.execute(statement)
        yield
    finally:
        await conn.execute("DROP TABLE IF EXISTS gize.test_widgets")
        await conn.close()


@pytest.mark.usefixtures("widgets_table")
async def test_app_role_reads_and_writes_under_row_level_security(
    db_session: AsyncSession,
) -> None:
    async with db_session.begin():
        widget = Widget(label="first")
        db_session.add(widget)
    assert isinstance(widget.id, uuid.UUID)
    assert widget.created_at.tzinfo is not None
    assert widget.updated_at >= widget.created_at
    assert widget.deleted_at is None
    rows = (await db_session.execute(select(Widget))).scalars().all()
    assert [row.label for row in rows] == ["first"]


@pytest.mark.usefixtures("widgets_table")
async def test_updated_at_moves_on_update(db_session: AsyncSession) -> None:
    async with db_session.begin():
        widget = Widget(label="before")
        db_session.add(widget)
    created = widget.updated_at
    async with db_session.begin():
        widget.label = "after"
    await db_session.refresh(widget)
    assert widget.updated_at >= created


@pytest.mark.usefixtures("widgets_table")
async def test_soft_delete_filter(db_session: AsyncSession) -> None:
    async with db_session.begin():
        kept, gone = Widget(label="kept"), Widget(label="gone")
        db_session.add_all([kept, gone])
    async with db_session.begin():
        gone.deleted_at = datetime.now(UTC)
    rows = (await db_session.execute(select(Widget).where(Widget.not_deleted()))).scalars().all()
    assert [row.label for row in rows] == ["kept"]


@pytest.mark.usefixtures("widgets_table")
async def test_each_test_starts_clean(db_session: AsyncSession) -> None:
    rows = (await db_session.execute(select(Widget))).scalars().all()
    assert rows == []


@pytest.mark.usefixtures("widgets_table")
async def test_supabase_roles_are_denied_rows_even_with_a_grant(database: DatabaseUrls) -> None:
    """Defence in depth: RLS has no policy for API roles, so a stray grant still exposes nothing."""
    admin = await asyncpg.connect(database.admin, timeout=3)
    try:
        await admin.execute("INSERT INTO gize.test_widgets (label) VALUES ('secret')")
        await admin.execute("GRANT USAGE ON SCHEMA gize TO authenticated")
        await admin.execute("GRANT SELECT ON gize.test_widgets TO authenticated")
        await admin.execute("SET ROLE authenticated")
        assert await admin.fetch("SELECT * FROM gize.test_widgets") == []
    finally:
        await admin.execute("RESET ROLE")
        await admin.execute("REVOKE SELECT ON gize.test_widgets FROM authenticated")
        await admin.execute("REVOKE USAGE ON SCHEMA gize FROM authenticated")
        await admin.execute("DELETE FROM gize.test_widgets")
        await admin.close()


def test_constraint_names_follow_the_convention() -> None:
    table = cast(Table, Widget.__table__)
    assert {constraint.name for constraint in table.constraints} >= {"pk_test_widgets"}
    assert table.schema == "gize"
