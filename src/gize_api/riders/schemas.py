"""Rider request and response bodies (GZ-034)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from gize_api.core.contact import MOBILE_E164_LENGTH, EthiopianMobile
from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalDate
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, SearchText, ShortText
from gize_api.core.updates import PartialUpdate
from gize_api.corridors.schemas import AreaRef
from gize_api.riders.models import RiderPlan, RiderSource, RiderType
from gize_api.routes.models import ROUTE_CODE_MAX_LENGTH, AssignmentStatus

MAX_LOOKUP = 20


class RiderCreate(BaseModel):
    """An adult recorded by staff (from a phone call). The phone may be typed any usual
    way (`0911 23 45 67`); it is stored `+251911234567`. Company riders come with GZ-035."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_name: ShortText
    phone: EthiopianMobile
    home_area_id: uuid.UUID = Field(description="Where they board.")
    destination_area_id: uuid.UUID = Field(description="Where they go.")
    plan: RiderPlan = Field(description="`morning`: A to B only; `evening`: B to A only.")
    saturday_addon: StrictBool = False

    @model_validator(mode="after")
    def _goes_somewhere(self) -> Self:
        if self.home_area_id == self.destination_area_id:
            raise ValueError("home and destination are the same area")
        return self


class RiderUpdate(PartialUpdate):
    """Change any of these (leave out what stays). A rider holding a seat must still fit
    its route: boarding at A or an area it serves, going to B, Saturday only where offered
    (a 422 on the field otherwise)."""

    full_name: ShortText | None = None
    phone: EthiopianMobile | None = None
    home_area_id: uuid.UUID | None = None
    destination_area_id: uuid.UUID | None = None
    plan: RiderPlan | None = None
    saturday_addon: StrictBool | None = None


class SeatState(StrEnum):
    RESERVED = "reserved"
    ACTIVE = "active"
    NONE = "none"


class SeatRef(BaseModel):
    """The seat the rider holds today or later (one at most)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: uuid.UUID
    route_code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)
    status: AssignmentStatus = Field(description="`reserved` (forming route) or `active`.")
    start_date: LocalDate
    end_date: LocalDate | None = Field(description="The last day held; null: ongoing.")


class _RiderBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    rider_type: RiderType
    full_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    home_area: AreaRef | None
    destination_area: AreaRef | None
    plan: RiderPlan | None
    saturday_addon: bool
    source: RiderSource = Field(description="`app`: signed up themselves; `staff`: recorded.")
    seat: SeatRef | None
    open_requests: int = Field(description="Their open route requests (new or matched).")
    created_at: datetime
    updated_at: datetime


class RiderRead(_RiderBase):
    phone: str | None = Field(max_length=MOBILE_E164_LENGTH, description="E.164, in full.")


class RiderListItem(_RiderBase):
    phone_masked: str | None = Field(
        max_length=MOBILE_E164_LENGTH, description="`+2519****4567`: enough to tell callers apart."
    )


class RiderPage(Page[RiderListItem]):
    """A page of riders (a named schema: generated clients keep the name)."""


class RiderListParams(PageParams):
    """Filters, combined with AND. `q`: names starting with it, ignoring case (blank: no
    filter). `seat`: holding a reserved or active seat today or later, or none. `route_id`:
    holding a seat on that route."""

    q: SearchText = None
    rider_type: RiderType | None = None
    seat: SeatState | None = None
    route_id: uuid.UUID | None = None


class RiderLookup(BaseModel):
    """A phone, typed any usual way: in the body, so it never reaches a URL or a log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phone: EthiopianMobile


class RiderMatches(BaseModel):
    """Live riders with this phone (at most one adult; company riders may share later)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[RiderListItem] = Field(max_length=MAX_LOOKUP)


def masked(phone: str | None) -> str | None:
    return None if phone is None else f"{phone[:5]}****{phone[-4:]}"
