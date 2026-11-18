"""Route request bodies."""

import uuid
from datetime import datetime, time
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from gize_api.core.contact import MOBILE_E164_LENGTH, EthiopianMobile
from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalTime
from gize_api.core.text import NOTES_MAX_LENGTH, SHORT_TEXT_MAX_LENGTH, Notes, ShortText
from gize_api.core.updates import PartialUpdate
from gize_api.corridors.schemas import AreaRef
from gize_api.route_requests.models import (
    RequestKind,
    RequestSource,
    RequestStatus,
    RiderType,
)
from gize_api.routes.models import ROUTE_CODE_MAX_LENGTH, RouteStatus


def preferred_times_in_order(morning: time | None, back: time | None) -> None:
    if morning is not None and back is not None and back <= morning:
        raise ValueError("the preferred return time comes after the morning time")


class RouteRequestCreate(BaseModel):
    """A new request, `new`, recorded by staff.

    - `new_areas`: each end once, a listed area (`*_area_id`) or one not listed yet
      (`*_area_name`); A and B different. No `route_id`.
    - `route`: `route_id`, a running (active or paused) route for this rider (public for
      adults; student, of the rider's school, for students). No areas.

    A student names their school; an adult doesn't. The phone may be typed any usual way.
    One open request per phone, kind, route or area pair and rider (`ROUTE_REQUEST_EXISTS`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RequestKind
    from_area_id: uuid.UUID | None = None
    from_area_name: ShortText | None = Field(default=None, description="An area not listed yet.")
    to_area_id: uuid.UUID | None = None
    to_area_name: ShortText | None = Field(default=None, description="An area not listed yet.")
    route_id: uuid.UUID | None = None
    rider_type: RiderType
    rider_name: ShortText = Field(description="Who will ride: a student's own name.")
    school_name: ShortText | None = None
    preferred_morning_time: LocalTime | None = None
    preferred_return_time: LocalTime | None = None
    contact_name: ShortText = Field(description="Who to call: a student's parent or guardian.")
    contact_phone: EthiopianMobile
    notes: Notes | None = None

    @model_validator(mode="after")
    def _asks_for_one_thing(self) -> Self:
        ends = (
            (self.from_area_id, self.from_area_name),
            (self.to_area_id, self.to_area_name),
        )
        if self.kind is RequestKind.ROUTE:
            if self.route_id is None or any(v is not None for end in ends for v in end):
                raise ValueError("a route request names its route and no areas")
        else:
            if self.route_id is not None:
                raise ValueError("a new-areas request names no route")
            if any((area_id is None) == (name is None) for area_id, name in ends):
                raise ValueError("each end is a listed area or a name, once")
            if self.from_area_id is not None and self.from_area_id == self.to_area_id:
                raise ValueError("A and B are different areas")
            names = (self.from_area_name, self.to_area_name)
            if None not in names and names[0].lower() == names[1].lower():  # type: ignore[union-attr]
                raise ValueError("A and B are different areas")
        if (self.school_name is not None) != (self.rider_type is RiderType.STUDENT):
            raise ValueError("a student, and only one, names their school")
        preferred_times_in_order(self.preferred_morning_time, self.preferred_return_time)
        return self


class RouteRequestUpdate(PartialUpdate):
    """Correct who and when (leave out what stays; empty notes clear them). What is asked
    for (kind, areas, route) doesn't change: decline the request and record a new one. A
    school only on a student's request (still the route's, for a route request)."""

    rider_name: ShortText | None = None
    school_name: ShortText | None = None
    preferred_morning_time: LocalTime | None = None
    preferred_return_time: LocalTime | None = None
    contact_name: ShortText | None = None
    contact_phone: EthiopianMobile | None = None
    notes: Notes | None = None


class RouteRequestStatusSet(BaseModel):
    """The status to move to. `matched` names the route (`matched_route_id`, a live route
    for this rider, any status: a van to come may still be a draft); `declined` gives a
    reason. Neither goes with another status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RequestStatus
    matched_route_id: uuid.UUID | None = Field(default=None, validate_default=True)
    decline_reason: ShortText | None = Field(default=None, validate_default=True)

    @field_validator("matched_route_id", "decline_reason")
    @classmethod
    def _goes_with_its_status(cls, value: object, info: ValidationInfo) -> object:
        if "status" not in info.data:  # a bad status has its own error
            return value
        needs = {
            "matched_route_id": RequestStatus.MATCHED,
            "decline_reason": RequestStatus.DECLINED,
        }
        if (value is not None) != (info.data["status"] is needs[str(info.field_name)]):
            raise ValueError(f"required with status {needs[str(info.field_name)]}, else not sent")
        return value


class RouteRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)


class RouteRequestRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    kind: RequestKind
    from_area: AreaRef | None
    from_area_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    to_area: AreaRef | None
    to_area_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    route: RouteRef | None = Field(description="The route asked for (kind `route`).")
    rider_type: RiderType
    rider_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    school_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    preferred_morning_time: LocalTime | None
    preferred_return_time: LocalTime | None
    contact_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    contact_phone: str = Field(max_length=MOBILE_E164_LENGTH, description="E.164, +2519…/+2517…")
    notes: str | None = Field(max_length=NOTES_MAX_LENGTH)
    source: RequestSource
    status: RequestStatus
    matched_route: RouteRef | None
    decline_reason: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    created_at: datetime
    updated_at: datetime


class RouteRequestPage(Page[RouteRequestRead]):
    """A page of requests (a named schema: generated clients keep the name)."""


class RouteRequestListParams(PageParams):
    """Filters, combined with AND. `open`: `new` or `matched` (true) or closed (false);
    `route_id`: requests asking for that route or matched to it.

    Each end (GZ-076): a listed area (`*_area_id`) or one typed as not listed
    (`*_area_name`, the same name ignoring case, as the summary groups it), never both.
    A typed name finds typed ends only."""

    status: RequestStatus | None = None
    kind: RequestKind | None = None
    open: bool | None = None
    route_id: uuid.UUID | None = None
    from_area_id: uuid.UUID | None = Field(default=None, description="A listed area at A.")
    from_area_name: ShortText | None = Field(
        default=None, description="An area typed as not listed at A, ignoring case."
    )
    to_area_id: uuid.UUID | None = Field(default=None, description="A listed area at B.")
    to_area_name: ShortText | None = Field(
        default=None, description="An area typed as not listed at B, ignoring case."
    )

    @model_validator(mode="after")
    def _one_way_to_name_each_end(self) -> Self:
        if self.from_area_id is not None and self.from_area_name is not None:
            raise ValueError("from_area_id or from_area_name, not both")
        if self.to_area_id is not None and self.to_area_name is not None:
            raise ValueError("to_area_id or to_area_name, not both")
        return self


class AreaPairDemand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_area: AreaRef | None
    from_area_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    to_area: AreaRef | None
    to_area_name: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    open: int


class RouteDemand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: RouteRef
    status: RouteStatus
    open: int
    seats_free_next_month: int


class RouteRequestSummary(BaseModel):
    """Busiest first, at most 100 of each (`*_truncated`: there were more)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    area_pairs: list[AreaPairDemand] = Field(
        max_length=100, description="Open new-areas requests not matched yet, per area pair."
    )
    area_pairs_truncated: bool
    routes: list[RouteDemand] = Field(
        max_length=100,
        description="Open requests per route: matched to it, else asking for it.",
    )
    routes_truncated: bool
