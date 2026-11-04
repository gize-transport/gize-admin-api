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
from gize_api.routes.schemas import CompanyRef

MAX_LOOKUP = 20


class RecordedType(StrEnum):
    """The riders staff record: adults (who may also sign up themselves) and company
    riders."""

    ADULT = "adult"
    CORPORATE = "corporate"


class RiderCreate(BaseModel):
    """A rider recorded by staff. The phone may be typed any usual way (`0911 23 45 67`);
    it is stored `+251911234567`.

    - `adult` (from a phone call): home and destination areas, plan, Saturday add-on.
    - `corporate` (GZ-035): their `company_id` and where they board (`home_area_id`); no
      destination (they ride to the route's B), plan or Saturday (the company is billed).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rider_type: RecordedType = RecordedType.ADULT
    full_name: ShortText
    phone: EthiopianMobile
    company_id: uuid.UUID | None = Field(default=None, description="Company riders only.")
    home_area_id: uuid.UUID = Field(description="Where they board.")
    destination_area_id: uuid.UUID | None = Field(
        default=None, description="Where they go (adults only)."
    )
    plan: RiderPlan | None = Field(
        default=None, description="Adults only. `morning`: A to B only; `evening`: B to A."
    )
    saturday_addon: StrictBool = False

    @model_validator(mode="after")
    def _fits_its_type(self) -> Self:
        if self.rider_type is RecordedType.CORPORATE:
            if self.company_id is None:
                raise ValueError("a company rider names their company")
            if self.destination_area_id is not None or self.plan is not None:
                raise ValueError("a company rider has no destination or plan")
            if self.saturday_addon:
                raise ValueError("a company rider has no Saturday add-on")
            return self
        if self.company_id is not None:
            raise ValueError("an adult has no company")
        if self.destination_area_id is None or self.plan is None:
            raise ValueError("an adult has a destination and a plan")
        if self.home_area_id == self.destination_area_id:
            raise ValueError("home and destination are the same area")
        return self


class RiderUpdate(PartialUpdate):
    """Change any of these (leave out what stays). A rider holding a seat must still fit
    its route: boarding at A or an area it serves, going to B, Saturday only where offered
    (a 422 on the field otherwise). Company riders change only their name, phone and where
    they board; their company doesn't change (record them again)."""

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
    """A seat the rider holds today or later."""

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
    company: CompanyRef | None = Field(description="Company riders only.")
    seat: SeatRef | None = Field(description="The seat held now, or the next to start.")
    next_seat: SeatRef | None = Field(
        description="While moving: the seat that starts after `seat` ends (GZ-035)."
    )
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


class SeatPlacement(BaseModel):
    """The route to place the rider on (or move them to).

    - `start_date`: an adult starts on a 1st (the next by default, at most a year ahead);
      a company rider on any day (today by default, at most a year ahead).
    - `skip_reason`: needed to place an adult on an active route ahead of other people's
      open requests for it (kept with the seat and audited).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: uuid.UUID
    start_date: LocalDate | None = None
    skip_reason: ShortText | None = None


class SeatEnd(BaseModel):
    """When the rider leaves: an adult at a month's end (this month's by default); a
    company rider on any day (today by default). A reservation, or a seat that hasn't
    started by then, ends at once (it never rode)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    end_date: LocalDate | None = Field(default=None, description="The last day they ride.")


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
