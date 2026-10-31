"""Local schedule times ("HH:MM", Addis Ababa), days ("YYYY-MM-DD") and today's date there."""

from datetime import UTC, date, datetime, time, tzinfo

import pytest
from pydantic import BaseModel, ValidationError

from gize_api.core import schedule
from gize_api.core.schedule import LocalDate, LocalTime


class Timetable(BaseModel):
    at: LocalTime


class Launch(BaseModel):
    on: LocalDate


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


def test_days_are_yyyy_mm_dd() -> None:
    launch = Launch.model_validate({"on": "2026-12-01"})
    assert launch.on == date(2026, 12, 1)
    assert launch.model_dump(mode="json") == {"on": "2026-12-01"}
    assert Launch.model_validate({"on": date(2026, 12, 1)}).on == date(2026, 12, 1)


@pytest.mark.parametrize(
    "typed",
    [
        "2026-12-01T00:00:00",  # pydantic alone takes a midnight timestamp
        "2026-12-01Z",
        "2026/12/01",
        "2026-1-01",
        "\uff12\uff10\uff12\uff16-12-01",  # full-width digits
        "2026-02-30",  # no such day
        "2026-13-01",
        "",
        1796083200,  # pydantic alone takes a Unix time at midnight
        datetime(2026, 12, 1, tzinfo=UTC),
        None,
    ],
    ids=repr,
)
def test_other_days_are_refused(typed: object) -> None:
    with pytest.raises(ValidationError):
        Launch.model_validate({"on": typed})


def test_today_is_addis_ababas(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC+3 all year: 22:30 UTC is already the next day in Addis Ababa."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "Clock":
            return cls(2026, 11, 14, 22, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(schedule, "datetime", Clock)
    assert schedule.addis_today() == date(2026, 11, 15)
