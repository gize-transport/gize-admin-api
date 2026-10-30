"""Local schedule times ("HH:MM", Addis Ababa) and today's date there."""

from datetime import UTC, date, datetime, time, tzinfo

import pytest
from pydantic import BaseModel, ValidationError

from gize_api.core import schedule
from gize_api.core.schedule import LocalTime


class Timetable(BaseModel):
    at: LocalTime


@pytest.mark.parametrize(
    ("typed", "parsed"), [("06:30", time(6, 30)), ("00:00", time(0, 0)), ("23:59", time(23, 59))]
)
def test_times_are_hh_mm(typed: str, parsed: time) -> None:
    timetable = Timetable.model_validate({"at": typed})
    assert timetable.at == parsed
    assert timetable.model_dump(mode="json") == {"at": typed}


@pytest.mark.parametrize("typed", ["6:30", "24:00", "06:60", "06:30:00", "0630", "", 630, None])
def test_other_text_is_refused(typed: object) -> None:
    with pytest.raises(ValidationError):
        Timetable.model_validate({"at": typed})


def test_today_is_addis_ababas(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC+3 all year: 22:30 UTC is already the next day in Addis Ababa."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "Clock":
            return cls(2026, 11, 14, 22, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", Clock)
    assert schedule.addis_today() == date(2026, 11, 15)
