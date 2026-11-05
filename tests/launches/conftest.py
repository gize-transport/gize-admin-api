"""Launch tests run on a fixed "today" in Addis Ababa: 15 November 2026."""

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import schedule
from gize_api.core.db import borrowed
from gize_api.main import GizeAPI

TODAY = date(2026, 11, 15)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr(schedule, "addis_today", lambda: TODAY)
    return TODAY


@pytest.fixture(autouse=True)
def background_sessions(api_app: GizeAPI, db_session: AsyncSession) -> None:
    """Launch messages are sent after the answer, on the app's own sessions: here, the
    test's session (rolled back after it)."""
    api_app.state.sessionmaker = borrowed(db_session)
