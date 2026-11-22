"""Seat offers (GZ-040): a free seat offered to an open route request, for 48 hours.

Staff offer a route's free seat (from a coming 1st on an active route; a reservation on a
forming one) to the oldest request waiting for it, or to another with a reason. The
person accepts on the website (phone verified) or by calling staff; the accepted offer
becomes their seat and fulfils the request. An open offer holds its seat until it is
accepted, withdrawn, or its 48 hours pass (`expires_at`): every way of taking a seat
counts open offers. Nothing runs on a timer: an `open` offer past `expires_at` is expired
wherever it is read, and stored `expired` when next touched.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.audit import AuditedEntity
from gize_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH

OFFER_OPEN_UNIQUE = "uq_seat_offers_request_id_open"


# Where a request stands in a route's offer queue (GZ-040, GZ-075): its group, then its age.
NEXT, PASSED_OVER, NOT_OFFERABLE = 0, 1, 2


@dataclass(frozen=True)
class QueueKey:
    """A request's place in a route's offer queue: group (NEXT, PASSED_OVER, NOT_OFFERABLE),
    then oldest first, then by id."""

    group: int
    created_at: datetime
    id: uuid.UUID


class NotOfferable(StrEnum):
    """Why a request waiting for a route can't be offered its seat (GZ-075): it has an open
    offer (on any route), or it's a student's (seats are offered to adults)."""

    OPEN_OFFER = "open_offer"
    STUDENT = "student"


class OfferStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


def _string_enum[E: StrEnum](kind: type[E]) -> Enum:
    return Enum(
        kind,
        native_enum=False,  # VARCHAR + a named check: a new value is a plain migration
        create_constraint=False,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        length=20,
    )


# The skip reason is free text: redacted.
SEAT_OFFER_AUDIT = AuditedEntity(
    type="seat_offer",
    fields=(
        "route_id",
        "request_id",
        "rider_id",
        "start_date",
        "status",
        "expires_at",
        "skip_reason",
    ),
    visible=frozenset({"route_id", "request_id", "rider_id", "start_date", "status", "expires_at"}),
)


class SeatOffer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "seat_offers"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'accepted', 'expired', 'withdrawn')", name="status"),
        CheckConstraint("(status = 'accepted') = (rider_id IS NOT NULL)", name="accepted"),
        # One offer at a time per request (an expired one is stored so when next touched).
        Index(
            OFFER_OPEN_UNIQUE,
            "request_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_seat_offers_route_id_created_at", "route_id", "created_at"),
        Index(  # open offers on every route, soonest to run out first (GZ-084)
            "ix_seat_offers_open_expires_at_id",
            "expires_at",
            "id",
            postgresql_where=text("status = 'open'"),
        ),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gize.routes.id", ondelete="RESTRICT"))
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gize.route_requests.id", ondelete="RESTRICT")
    )
    # Who took the seat (set on acceptance; created from the request if new).
    rider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.riders.id", ondelete="RESTRICT")
    )
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[OfferStatus] = mapped_column(
        _string_enum(OfferStatus), default=OfferStatus.OPEN, server_default=OfferStatus.OPEN.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    skip_reason: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
