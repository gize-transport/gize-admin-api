"""Public sign-up bodies (GZ-032)."""

import uuid
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gize_api.core.schedule import LocalDate
from gize_api.core.text import ShortText
from gize_api.riders.models import RiderPlan

TOKEN_MAX_LENGTH = 2000


class AdultRegistration(BaseModel):
    """An adult signing themselves up. The phone is the one the token proves (GZ-031)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_phone_token: str = Field(
        min_length=1,
        max_length=TOKEN_MAX_LENGTH,
        description="From `POST /v1/verification/verify`: valid 30 minutes, for one sign-up.",
    )
    full_name: ShortText
    home_area_id: uuid.UUID = Field(description="Where they board: A, or an area served.")
    destination_area_id: uuid.UUID = Field(description="Where they go: B.")
    plan: RiderPlan = Field(description="`morning`: A to B only; `evening`: B to A only.")
    saturday: bool = Field(
        default=False, strict=True, description="The Saturday add-on (a route offering it)."
    )
    route_id: uuid.UUID | None = Field(
        default=None,
        description="A route from `GET /v1/public/routes` for these areas. None: ask for a "
        "route between them.",
    )

    @model_validator(mode="after")
    def _goes_somewhere(self) -> Self:
        if self.home_area_id == self.destination_area_id:
            raise ValueError("home and destination are the same area")
        return self


class RegistrationOutcome(StrEnum):
    RESERVED = "reserved"
    REQUESTED = "requested"


class AdultRegistered(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rider_id: uuid.UUID
    outcome: RegistrationOutcome = Field(
        description="`reserved`: a seat on a forming route, from `start_date` once it "
        "launches. `requested`: a request for the route (or the areas); Gize offers a seat "
        "when one frees up or a route starts."
    )
    route_id: uuid.UUID | None = Field(description="The route reserved on or asked for.")
    start_date: LocalDate | None = Field(
        description="Reserved only: the 1st the seat is held from (riding starts when the "
        "route launches, on this day or a later 1st)."
    )
    route_request_id: uuid.UUID | None = Field(description="Requested only.")
