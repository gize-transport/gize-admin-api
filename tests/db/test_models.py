"""GZ-006: base model mixins and row-level security, exercised as the app role."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import asyncpg
import pytest
from sqlalchemy import String, Table, event, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.rls import row_level_security_statements
from tests.factories import Factory
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
async def test_every_update_sets_updated_at(db_session: AsyncSession) -> None:
    """`now()` is constant inside the test transaction, so check the statement itself."""
    async with db_session.begin():
        widget = Widget(label="before")
        db_session.add(widget)
    statements: list[str] = []

    def capture(*args: object) -> None:
        statements.append(str(args[2]))

    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    sync_engine = bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        async with db_session.begin():
            widget.label = "after"
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    (update,) = [statement for statement in statements if statement.startswith("UPDATE")]
    assert "updated_at=now()" in update


class WidgetFactory(Factory[Widget]):
    model = Widget

    def defaults(self, sequence: int) -> dict[str, object]:
        return {"label": f"widget-{sequence}"}


@pytest.mark.usefixtures("widgets_table")
async def test_factory_creates_valid_rows_with_overrides(db_session: AsyncSession) -> None:
    factory = WidgetFactory()
    first = await factory.create(db_session)
    second = await factory.create(db_session, label="chosen")
    assert isinstance(first.id, uuid.UUID)  # flushed: server defaults are loaded
    assert first.label != second.label
    assert second.label == "chosen"
    many = await factory.create_batch(db_session, 3)
    assert len({widget.label for widget in many}) == 3
    with pytest.raises(TypeError):
        await factory.create(db_session, no_such_column="x")


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


@pytest.mark.parametrize(
    ("role", "rows_seen"), [("anon", 0), ("authenticated", 0), ("service_role", 1)]
)
@pytest.mark.usefixtures("widgets_table")
async def test_what_a_stray_grant_to_a_supabase_role_would_expose(
    database: DatabaseUrls, role: str, rows_seen: int
) -> None:
    """RLS has no policy for API roles, so a stray grant exposes no rows to anon or
    authenticated. service_role bypasses RLS on Supabase: for it, having no access to the
    schema (test_privileges) is the only barrier."""
    admin = await asyncpg.connect(database.admin, timeout=3)
    try:
        await admin.execute("INSERT INTO gize.test_widgets (label) VALUES ('secret')")
        # `role` is a fixed test constant; DDL cannot take bind parameters.
        await admin.execute(f"GRANT USAGE ON SCHEMA gize TO {role}")
        await admin.execute(f"GRANT SELECT ON gize.test_widgets TO {role}")
        await admin.execute(f"SET ROLE {role}")
        assert len(await admin.fetch("SELECT * FROM gize.test_widgets")) == rows_seen
    finally:
        await admin.execute("RESET ROLE")
        await admin.execute(f"REVOKE SELECT ON gize.test_widgets FROM {role}")  # noqa: S608
        await admin.execute(f"REVOKE USAGE ON SCHEMA gize FROM {role}")
        await admin.execute("DELETE FROM gize.test_widgets")
        await admin.close()


def test_constraint_names_follow_the_convention() -> None:
    table = cast(Table, Widget.__table__)
    assert {constraint.name for constraint in table.constraints} >= {"pk_test_widgets"}
    assert table.schema == "gize"
