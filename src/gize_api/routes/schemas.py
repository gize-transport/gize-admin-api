"""Route request and response bodies."""

import uuid
from datetime import date, datetime, time
from itertools import pairwise
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalDate, LocalTime
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText
from gize_api.core.updates import PartialUpdate
from gize_api.corridors.schemas import AreaRef
from gize_api.routes.models import (
    MAX_PRICE,
    MAX_SEATS,
    MAX_SERVED_AREAS,
    ROUTE_CODE_MAX_LENGTH,
    RouteStatus,
    RouteType,
)

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
    neither. Times in order: leave A, arrive at B, leave B, arrive at A. Prices are set by
    admins (GZ-022); the areas served by their own endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: RouteCode
    corridor_id: uuid.UUID
    route_type: RouteType
    school_name: ShortText | None = None
    company_id: uuid.UUID | None = None
    morning_time: LocalTime = Field(description="Leaves A.")
    morning_arrival_time: LocalTime = Field(description="Arrives at B.")
    return_time: LocalTime = Field(description="Leaves B.")
    return_arrival_time: LocalTime = Field(description="Arrives at A.")
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
        times_in_order(
            self.morning_time, self.morning_arrival_time, self.return_time, self.return_arrival_time
        )
        return self


class RouteUpdate(PartialUpdate):
    """Change any of these (leave out what stays). The type can't change; corridor, school
    and company can't while riders hold seats (`ROUTE_HAS_RIDERS`), and the corridor not
    while the route serves areas (clear them first). A route made before arrival times
    existed gets both at once."""

    code: RouteCode | None = None
    corridor_id: uuid.UUID | None = None
    school_name: ShortText | None = None
    company_id: uuid.UUID | None = None
    morning_time: LocalTime | None = Field(default=None, description="Leaves A.")
    morning_arrival_time: LocalTime | None = Field(default=None, description="Arrives at B.")
    return_time: LocalTime | None = Field(default=None, description="Leaves B.")
    return_arrival_time: LocalTime | None = Field(default=None, description="Arrives at A.")
    seats: Seats | None = None
    launch_minimum: LaunchMinimum | None = None

    @model_validator(mode="after")
    def _minimum_within_seats(self) -> Self:
        seats, minimum = self.seats, self.launch_minimum
        if seats is not None and minimum is not None and minimum > seats:
            raise ValueError("the launch minimum can't be more than the seats")
        return self


Price = Annotated[int, Field(ge=1, le=MAX_PRICE, strict=True, description="Whole birr a month.")]


class RoutePricesSet(BaseModel):
    """All of a route's prices at once (admins). No Saturday price (left out or null) means
    no Saturday add-on; only public routes have one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_trip: Price
    one_way: Price
    saturday: Price | None = None


class RouteAreasSet(BaseModel):
    """The areas the route passes between A and B, in order: the whole list (empty: none).
    Not A or B themselves (the corridor's); each once; at most 20."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    area_ids: list[uuid.UUID] = Field(max_length=MAX_SERVED_AREAS)


class RouteStatusSet(BaseModel):
    """The status to move the route to. Launching a forming route (`active`) needs a
    `launch_date`: the 1st of a month, not before today; no other move takes one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RouteStatus
    launch_date: LocalDate | None = Field(
        default=None,
        description="Launching only: the day reserved riders start (the 1st of a month).",
    )

    @field_validator("launch_date")
    @classmethod
    def _launches_on_the_1st(cls, value: date | None, info: ValidationInfo) -> date | None:
        if value is None:
            return value
        if info.data.get("status") is not RouteStatus.ACTIVE:
            raise ValueError("a launch date comes only with status active")
        if value.day != 1:
            raise ValueError("routes launch on the 1st of a month")
        return value


def times_in_order(*times: time | None) -> None:
    """Leave A, arrive at B, leave B, arrive at A: each after the one before (None: not set
    on an older route)."""
    known = [moment for moment in times if moment is not None]
    if any(later <= earlier for earlier, later in pairwise(known)):
        raise ValueError("leave A, arrive at B, leave B, arrive at A: in that order")


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
    morning_time: LocalTime = Field(description="Leaves A.")
    morning_arrival_time: LocalTime | None = Field(
        description="Arrives at B (null only on routes made before arrival times existed)."
    )
    return_time: LocalTime = Field(description="Leaves B.")
    return_arrival_time: LocalTime | None = Field(
        description="Arrives at A (null only on routes made before arrival times existed)."
    )
    served_areas: list[AreaRef] = Field(description="Areas passed between A and B, in order.")
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
