"""Seat offer bodies (GZ-040)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalDate
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText
from gize_api.offers.models import OfferStatus
from gize_api.registrations.schemas import TOKEN_MAX_LENGTH
from gize_api.routes.models import ROUTE_CODE_MAX_LENGTH
from gize_api.sms.models import SmsStatus


class OfferCreate(BaseModel):
    """Offer the route's free seat. Leave `request_id` out for the oldest request waiting
    for the route; another one ahead of older ones needs `skip_reason` (audited).
    `start_date`: on an active route, the 1st the seat is free from (the next by default);
    a forming route's offer is a reservation from the next 1st."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID | None = None
    start_date: LocalDate | None = None
    skip_reason: ShortText | None = None


class OfferRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    route_id: uuid.UUID
    route_code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)
    request_id: uuid.UUID
    rider_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH, description="Who asked.")
    start_date: LocalDate
    status: OfferStatus = Field(
        description="`open` (holds the seat until `expires_at`), `accepted`, `expired` or "
        "`withdrawn`."
    )
    expires_at: datetime
    skip_reason: str | None = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    rider_id: uuid.UUID | None = Field(description="Accepted: the rider who took the seat.")
    sms_status: SmsStatus | None = Field(
        description="The offer's SMS: `sent`, `failed` (call them) or `sending`."
    )
    created_at: datetime
    updated_at: datetime


class OfferPage(Page[OfferRead]):
    """A page of offers (a named schema: generated clients keep the name)."""


class OfferListParams(PageParams):
    """Newest first."""


class PublicOfferAccept(BaseModel):
    """The asker's phone, verified (GZ-031): it must be the request's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_phone_token: str = Field(min_length=1, max_length=TOKEN_MAX_LENGTH)


class PublicOfferAccepted(BaseModel):
    """What the website shows: nothing about anyone else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OfferStatus
    route_code: str = Field(max_length=ROUTE_CODE_MAX_LENGTH)
    start_date: LocalDate
