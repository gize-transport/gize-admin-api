"""What the rider app reads before a sign-up (GZ-036): areas and the public routes between
two of them. Only what a rider needs: no seat counts, launch progress or staff fields."""

import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalTime
from gize_api.core.text import SearchText
from gize_api.corridors.schemas import AreaRef
from gize_api.routes.models import ROUTE_CODE_MAX_LENGTH, RouteStatus


class PublicAreaPage(Page[AreaRef]):
    """A page of areas (a named schema: generated clients keep the name)."""


class PublicAreaListParams(PageParams):
    """`q`: names starting with it, ignoring case. Blank means no filter."""

    q: SearchText = None


class PublicRouteListParams(PageParams):
    """The trip: where the rider boards and where they go, two different areas."""

    from_area_id: uuid.UUID
    to_area_id: uuid.UUID

    @model_validator(mode="after")
    def _goes_somewhere(self) -> Self:
        if self.from_area_id == self.to_area_id:
            raise ValueError("from and to are the same area")
        return self


class PublicPrices(BaseModel):
    """Whole birr a month. Saturday: an add-on, when the route offers one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_trip: int
    one_way: int
    saturday: int | None


class PublicRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)
    origin_area: AreaRef = Field(description="A: where the morning run starts.")
    destination_area: AreaRef = Field(description="B: where the morning run ends.")
    served_areas: list[AreaRef] = Field(description="Areas passed between A and B, in order.")
    morning_time: LocalTime = Field(description="Leaves A.")
    morning_arrival_time: LocalTime | None = Field(
        description="Arrives at B (null only on routes made before arrival times existed)."
    )
    return_time: LocalTime = Field(description="Leaves B.")
    return_arrival_time: LocalTime | None = Field(
        description="Arrives at A (null only on routes made before arrival times existed)."
    )
    prices: PublicPrices
    status: RouteStatus = Field(
        description="`forming`: a sign-up reserves a seat until it launches (unless full). "
        "`active` or `paused`: a sign-up becomes a request for a seat."
    )
    full: bool = Field(description="No free seat this month or next.")


class PublicRoutePage(Page[PublicRoute]):
    """A page of public routes (a named schema: generated clients keep the name)."""
