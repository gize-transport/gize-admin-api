"""Route requests (GZ-027): unmet demand from individual riders, recorded by staff from
phone calls (later by the public app, phone-verified).

There is no waitlist (owner decision, GZ-025). A request is either for two areas no route
serves yet (`new_areas`; either area may be one not listed yet, typed as a name) or for a
specific running route (`route`, usually full). Staff match it to a route (a forming one, or
a van to come), mark it fulfilled once the person has a place, or decline it with a reason.
`new` and `matched` requests are open.

One open request per phone, kind, route or area pair and rider (`uq_route_requests_open`):
siblings are separate requests. The rider's and contact's names, the phone, the school, the
notes and the decline reason are personal data: the audit log keeps only that they changed.
"""

import uuid
from datetime import time
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Time, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gize_api.areas.models import Area
from gize_api.core.audit import AuditedEntity
from gize_api.core.contact import MOBILE_E164_LENGTH
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import NOTES_MAX_LENGTH, SHORT_TEXT_MAX_LENGTH
from gize_api.routes.models import Route

ROUTE_REQUEST_OPEN_UNIQUE = "uq_route_requests_open"


class RequestKind(StrEnum):
    NEW_AREAS = "new_areas"
    ROUTE = "route"


class RiderType(StrEnum):
    """Individual riders only: company riders come through their company."""

    ADULT = "adult"
    STUDENT = "student"


class RequestSource(StrEnum):
    STAFF = "staff"
    APP = "app"  # the public app, later


class RequestStatus(StrEnum):
    NEW = "new"
    MATCHED = "matched"
    FULFILLED = "fulfilled"
    DECLINED = "declined"


OPEN_STATUSES = (RequestStatus.NEW, RequestStatus.MATCHED)


def _string_enum[E: StrEnum](kind: type[E]) -> Enum:
    return Enum(
        kind,
        native_enum=False,  # VARCHAR + a named check: a new value is a plain migration
        create_constraint=False,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        length=20,
    )


_TEXT_FIELDS = (
    "from_area_name",
    "to_area_name",
    "rider_name",
    "school_name",
    "contact_name",
    "notes",
    "decline_reason",
)
# Names, the phone, the school, notes and the decline reason are personal data: not shown.
ROUTE_REQUEST_AUDIT = AuditedEntity(
    type="route_request",
    fields=(
        "kind",
        "from_area_id",
        "from_area_name",
        "to_area_id",
        "to_area_name",
        "route_id",
        "rider_type",
        "rider_name",
        "school_name",
        "preferred_morning_time",
        "preferred_return_time",
        "contact_name",
        "contact_phone",
        "notes",
        "source",
        "status",
        "matched_route_id",
        "decline_reason",
        "deleted_at",
    ),
    visible=frozenset(
        {
            "kind",
            "from_area_id",
            "from_area_name",
            "to_area_id",
            "to_area_name",
            "route_id",
            "rider_type",
            "preferred_morning_time",
            "preferred_return_time",
            "source",
            "status",
            "matched_route_id",
            "deleted_at",
        }
    ),
)


class RouteRequest(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "route_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('new_areas', 'route')", name="kind"),
        # A route request names its route and no areas; a new-areas request names no route
        # and each end once, listed (id) or not yet (name).
        CheckConstraint(
            "(kind = 'route' AND route_id IS NOT NULL AND from_area_id IS NULL"
            " AND from_area_name IS NULL AND to_area_id IS NULL AND to_area_name IS NULL)"
            " OR (kind = 'new_areas' AND route_id IS NULL"
            " AND (from_area_id IS NULL) <> (from_area_name IS NULL)"
            " AND (to_area_id IS NULL) <> (to_area_name IS NULL))",
            name="kind_fields",
        ),
        CheckConstraint(
            "(from_area_id IS NULL OR to_area_id IS NULL OR from_area_id <> to_area_id)"
            " AND (from_area_name IS NULL OR to_area_name IS NULL"
            " OR lower(from_area_name) <> lower(to_area_name))",
            name="ends",
        ),
        *(
            CheckConstraint(
                f"{field} IS NULL OR ({field} = btrim({field}) AND {field} <> '')", name=field
            )
            for field in _TEXT_FIELDS
        ),
        CheckConstraint("rider_type IN ('adult', 'student')", name="rider_type"),
        CheckConstraint("(rider_type = 'student') = (school_name IS NOT NULL)", name="school"),
        CheckConstraint("contact_phone ~ '^\\+251[79][0-9]{8}$'", name="contact_phone"),
        CheckConstraint(
            "preferred_morning_time IS NULL OR preferred_return_time IS NULL"
            " OR preferred_morning_time < preferred_return_time",
            name="preferred_times",
        ),
        CheckConstraint("source IN ('staff', 'app')", name="source"),
        CheckConstraint(
            "status IN ('new', 'matched', 'fulfilled', 'declined')"
            " AND (status <> 'matched' OR matched_route_id IS NOT NULL)"
            " AND (status <> 'new' OR matched_route_id IS NULL)"
            " AND (status = 'declined') = (decline_reason IS NOT NULL)",
            name="status",
        ),
        Index(
            ROUTE_REQUEST_OPEN_UNIQUE,
            "contact_phone",
            "kind",
            text("coalesce(route_id::text, '')"),
            text("coalesce(from_area_id::text, lower(from_area_name), '')"),
            text("coalesce(to_area_id::text, lower(to_area_name), '')"),
            func.lower(text("rider_name")),
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND status IN ('new', 'matched')"),
        ),
        Index("ix_route_requests_created_at_id", "created_at", "id"),  # the list's order
        Index("ix_route_requests_route_id", "route_id"),  # "route in use", filter
        Index("ix_route_requests_matched_route_id", "matched_route_id"),
        Index("ix_route_requests_from_area_id", "from_area_id"),  # "area in use"
        Index("ix_route_requests_to_area_id", "to_area_id"),
        Index("ix_route_requests_rider_id", "rider_id"),
    )

    kind: Mapped[RequestKind] = mapped_column(_string_enum(RequestKind))
    from_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    from_area_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    to_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    to_area_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.routes.id", ondelete="RESTRICT")
    )
    rider_type: Mapped[RiderType] = mapped_column(_string_enum(RiderType))
    # The rider who asked from the public app (GZ-032); none for staff-recorded requests.
    rider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.riders.id", ondelete="RESTRICT")
    )
    rider_name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    school_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    preferred_morning_time: Mapped[time | None] = mapped_column(Time)
    preferred_return_time: Mapped[time | None] = mapped_column(Time)
    contact_name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    contact_phone: Mapped[str] = mapped_column(String(MOBILE_E164_LENGTH))
    notes: Mapped[str | None] = mapped_column(String(NOTES_MAX_LENGTH))
    source: Mapped[RequestSource] = mapped_column(
        _string_enum(RequestSource),
        default=RequestSource.STAFF,
        server_default=RequestSource.STAFF.value,
    )
    status: Mapped[RequestStatus] = mapped_column(
        _string_enum(RequestStatus),
        default=RequestStatus.NEW,
        server_default=RequestStatus.NEW.value,
    )
    matched_route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.routes.id", ondelete="RESTRICT")
    )
    decline_reason: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    # Loaded with the request (repository); never lazily, which async can't do.
    from_area: Mapped[Area | None] = relationship(foreign_keys=[from_area_id], lazy="raise")
    to_area: Mapped[Area | None] = relationship(foreign_keys=[to_area_id], lazy="raise")
    route: Mapped[Route | None] = relationship(foreign_keys=[route_id], lazy="raise")
    matched_route: Mapped[Route | None] = relationship(
        foreign_keys=[matched_route_id], lazy="raise"
    )
