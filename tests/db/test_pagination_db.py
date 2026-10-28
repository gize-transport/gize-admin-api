"""GZ-008: keyset pagination over real rows: complete, stable, never repeating."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.pagination import PageParams, paginate
from tests.db.widgets import Widget

pytestmark = pytest.mark.usefixtures("widgets_table")


async def _walk(session: AsyncSession, limit: int, *, newest_first: bool = False) -> list[str]:
    labels: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        async with session.begin():
            page = await paginate(
                session,
                select(Widget),
                PageParams(limit=limit, cursor=cursor),
                created_at=Widget.created_at,
                id=Widget.id,
                newest_first=newest_first,
            )
        labels += [widget.label for widget in page.items]
        cursor = page.next_cursor
        if cursor is None:
            return labels
    raise AssertionError("pagination did not end")


async def test_pages_cover_every_row_once_even_with_equal_timestamps(
    db_session: AsyncSession,
) -> None:
    same_instant = datetime(2026, 10, 28, 6, 30, tzinfo=UTC)
    async with db_session.begin():
        db_session.add_all(
            [Widget(label=f"w{n:02d}", created_at=same_instant) for n in range(7)]
            + [
                Widget(label=f"x{n:02d}", created_at=datetime(2026, 10, 29, tzinfo=UTC))
                for n in range(3)
            ]
        )
    for limit in (1, 3, 10, 100):
        labels = await _walk(db_session, limit)
        assert sorted(labels) == sorted(
            [f"w{n:02d}" for n in range(7)] + [f"x{n:02d}" for n in range(3)]
        )
        assert len(labels) == len(set(labels)) == 10
        assert all(label.startswith("w") for label in labels[:7])  # oldest first


async def test_newest_first(db_session: AsyncSession) -> None:
    async with db_session.begin():
        for day in (1, 2, 3):
            db_session.add(Widget(label=f"d{day}", created_at=datetime(2026, 10, day, tzinfo=UTC)))
    assert await _walk(db_session, 2, newest_first=True) == ["d3", "d2", "d1"]


async def test_the_last_page_has_no_cursor(db_session: AsyncSession) -> None:
    async with db_session.begin():
        db_session.add_all([Widget(label="a"), Widget(label="b")])
    async with db_session.begin():
        page = await paginate(
            db_session,
            select(Widget),
            PageParams(limit=2),
            created_at=Widget.created_at,
            id=Widget.id,
        )
    assert len(page.items) == 2
    assert page.next_cursor is None
