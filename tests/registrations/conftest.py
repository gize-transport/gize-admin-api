"""Registration tests run on a fixed "today" in Addis Ababa: 15 November 2026."""

from datetime import date

import pytest

from gize_api.core import schedule

TODAY = date(2026, 11, 15)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr(schedule, "addis_today", lambda: TODAY)
    return TODAY
