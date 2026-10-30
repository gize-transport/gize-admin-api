"""Schedule times and dates, in Addis Ababa.

- `LocalTime`: a time of day as `"HH:MM"`, local Addis Ababa time (api-contract.md): a daily
  timetable, not a moment, so not converted to UTC.
- `addis_today()`: today's date in Addis Ababa, the day seats and months are counted from.
  Ethiopia is UTC+3 all year (no daylight saving), so a fixed offset is exact and needs no
  time-zone database.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

ADDIS_ABABA = timezone(timedelta(hours=3), "EAT")
HH_MM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def addis_today() -> date:
    return datetime.now(UTC).astimezone(ADDIS_ABABA).date()


def _parse_hh_mm(value: object) -> object:
    """ "06:30" becomes time(6, 30); anything else is left for pydantic to refuse."""
    if not isinstance(value, str):
        return value if isinstance(value, time) else "invalid"
    hours, colon, minutes = value.partition(":")
    if (
        colon
        and len(hours) == len(minutes) == 2
        and hours.isdigit()
        and minutes.isdigit()
        and hours.isascii()
        and minutes.isascii()
        and int(hours) < 24
        and int(minutes) < 60
    ):
        return time(int(hours), int(minutes))
    return "invalid"


LocalTime = Annotated[
    time,
    BeforeValidator(_parse_hh_mm),
    PlainSerializer(lambda value: value.strftime("%H:%M"), return_type=str),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": HH_MM_PATTERN,
            "description": "Local Addis Ababa time, HH:MM.",
        }
    ),
]
