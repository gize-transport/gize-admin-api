"""Seat offer bodies (GZ-040) and a route's offer queue (GZ-075)."""

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.pagination import Page, PageParams
from gize_api.core.schedule import LocalDate
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText
from gize_api.offers.models import NEXT, NOT_OFFERABLE, NotOfferable, OfferStatus, QueueKey
from gize_api.registrations.schemas import TOKEN_MAX_LENGTH
from gize_api.route_requests.schemas import RouteRequestRead
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


class OfferQueueParams(PageParams):
    """The queue's cursor is its own (`queue_after`): the place of the last request served,
    group and age. It carries no secret and grants nothing."""

    @property
    def queue_after(self) -> QueueKey | None:
        """The decoded cursor; raises AppError(INVALID_CURSOR) if it was tampered with."""
        return None if self.cursor is None else decode_queue_cursor(self.cursor)


class OfferQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int | None = Field(
        ge=1,
        description='1 is whom "Offer a free seat" picks; another needs a `skip_reason`. '
        "`null` when the request can't be offered a seat (`not_offerable`).",
    )
    offered_before: bool = Field(
        description="Offered a seat on this route before that expired or was withdrawn. "
        "Among requests that can be offered a seat, these come after the others."
    )
    not_offerable: NotOfferable | None = Field(
        description="Why it can't be offered: `open_offer` (on any route) or `student` "
        "(seats are offered to adults)."
    )
    request: RouteRequestRead


class OfferQueuePage(Page[OfferQueueItem]):
    """A page of a route's offer queue (a named schema: generated clients keep the name)."""


def encode_queue_cursor(key: QueueKey) -> str:
    raw = json.dumps([key.group, key.created_at.isoformat(), str(key.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode_queue_cursor(token: str) -> QueueKey:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        group, created_at_text, id_text = json.loads(raw.decode())
        created_at = datetime.fromisoformat(created_at_text)
        if type(group) is not int or not NEXT <= group <= NOT_OFFERABLE:
            raise ValueError("no such group")
        if not isinstance(id_text, str):
            raise TypeError("an id that isn't text")
        if created_at.tzinfo is None:
            raise ValueError("no time zone")
        key = QueueKey(group, created_at.astimezone(UTC), uuid.UUID(id_text))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError, OverflowError):
        raise AppError(ErrorCode.INVALID_CURSOR) from None
    return key
