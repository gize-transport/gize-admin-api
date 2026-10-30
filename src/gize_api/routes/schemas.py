"""Route request and response bodies."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalTime
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText
from gize_api.core.updates import PartialUpdate
from gize_api.corridors.schemas import AreaRef
from gize_api.routes.models import MAX_SEATS, ROUTE_CODE_MAX_LENGTH, RouteStatus, RouteType

# Letters (either case), digits and dashes, 2 to 20; stored upper-case: "bole-mex-01" is
# "BOLE-MEX-01" (the table's check is upper-case only).
ROUTE_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,18}[A-Za-z0-9]$"

RouteCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_upper=True,
        min_length=2,
        max_length=ROUTE_CODE_MAX_LENGTH,
        pattern=ROUTE_CODE_PATTERN,
    ),
]
Seats = Annotated[int, Field(ge=1, le=MAX_SEATS, strict=True)]
LaunchMinimum = Annotated[int, Field(ge=1, le=MAX_SEATS, strict=True)]


class RouteCreate(BaseModel):
    """A new route, `draft`. Its type is fixed: a student route names its school, a
    corporate route its (active) company and has no launch minimum; a public route has
    neither. The morning time comes before the return. Prices are set by admins (GZ-022)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: RouteCode
    corridor_id: uuid.UUID
    route_type: RouteType
    school_name: ShortText | None = None
    company_id: uuid.UUID | None = None
    morning_time: LocalTime
    return_time: LocalTime
    seats: Seats
    launch_minimum: LaunchMinimum | None = None

    @model_validator(mode="after")
    def _fits_its_type(self) -> Self:
        student, corporate = (
            self.route_type is RouteType.STUDENT,
            self.route_type is RouteType.CORPORATE,
        )
        if (self.school_name is not None) != student:
            raise ValueError("a student route, and only one, names its school")
        if (self.company_id is not None) != corporate:
            raise ValueError("a corporate route, and only one, belongs to a company")
        if (self.launch_minimum is None) != corporate:
            raise ValueError(
                "public and student routes have a launch minimum; corporate ones don't"
            )
        if self.launch_minimum is not None and self.launch_minimum > self.seats:
            raise ValueError("the launch minimum can't be more than the seats")
        if self.morning_time >= self.return_time:
            raise ValueError("the morning time comes before the return time")
        return self


class RouteUpdate(PartialUpdate):
    """Change any of these (leave out what stays). The type can't change; corridor, school
    and company can't while riders hold seats (`ROUTE_HAS_RIDERS`)."""

    code: RouteCode | None = None
    corridor_id: uuid.UUID | None = None
    school_name: ShortText | None = None
    company_id: uuid.UUID | None = None
    morning_time: LocalTime | None = None
    return_time: LocalTime | None = None
    seats: Seats | None = None
    launch_minimum: LaunchMinimum | None = None

    @model_validator(mode="after")
    def _minimum_within_seats(self) -> Self:
        seats, minimum = self.seats, self.launch_minimum
        if seats is not None and minimum is not None and minimum > seats:
            raise ValueError("the launch minimum can't be more than the seats")
        return self


class CorridorRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    origin_area: AreaRef
    destination_area: AreaRef


class CompanyRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)


class Prices(BaseModel):
    """Whole birr a month; null until an admin sets them (GZ-022). Saturday: public only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_trip: int | None
    one_way: int | None
    saturday: int | None


class MonthSeats(BaseModel):
    """On the busiest day of each: from today to the end of this month, and all of next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    this_month: int
    next_month: int


class RouteRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)
    corridor: CorridorRef
    route_type: RouteType
    school_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    company: CompanyRef | None
    morning_time: LocalTime
    return_time: LocalTime
    seats: int
    launch_minimum: int | None = Field(description="Reserved riders needed to launch.")
    prices: Prices | None = Field(description="Null on corporate routes (billed per company).")
    status: RouteStatus
    copied_from_route_id: uuid.UUID | None
    seats_taken: MonthSeats
    seats_free: MonthSeats
    reserved: int = Field(
        description="Riders reserved for the next launch day (the 1st): launch progress."
    )
    created_at: datetime
    updated_at: datetime


class RoutePage(Page[RouteRead]):
    """A page of routes (a named schema: generated clients keep the name)."""


class RouteListParams(PageParams):
    """Filters, combined with AND; `q` finds codes starting with it (blank: no filter)."""

    q: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=20)] = None
    route_type: RouteType | None = None
    status: RouteStatus | None = None
    corridor_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
