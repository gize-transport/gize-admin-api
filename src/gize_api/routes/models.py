"""Routes and the seats riders hold on them (GZ-021).

A route is one van running a corridor at a morning and a return time (local Addis Ababa).
Its type is fixed at creation (owner decision):
- `public` and `student` routes are for prepaid individuals: they have prices (GZ-022) and
  a launch minimum, and collect reservations while `forming` (GZ-024);
- `corporate` routes belong to one company: no prices on the route, no minimum, never
  `forming`.

A `RouteAssignment` holds a seat on every day from `start_date` to `end_date` (open-ended
while `end_date` is null), unless `ended`. Seats are counted per day, so company riders
swapping mid-month take one seat. GZ-023 adds the pickup point; M3 the rider, plan and
Saturday add-on. Assignments are never deleted: they end.
"""

import uuid
from datetime import date, time
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gize_api.companies.models import Company
from gize_api.core.audit import AuditedEntity
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH
from gize_api.corridors.models import Corridor

ROUTE_CODE_UNIQUE = "uq_routes_code"
ROUTE_CODE_MAX_LENGTH = 20
MAX_SEATS = 60
MAX_PRICE = 100_000  # whole birr a month


class RouteType(StrEnum):
    PUBLIC = "public"
    STUDENT = "student"
    CORPORATE = "corporate"


class RouteStatus(StrEnum):
    DRAFT = "draft"
    FORMING = "forming"
    ACTIVE = "active"
    PAUSED = "paused"


class AssignmentStatus(StrEnum):
    RESERVED = "reserved"  # on a forming route, until it launches
    ACTIVE = "active"
    ENDED = "ended"  # holds no seat


def _string_enum[E: StrEnum](kind: type[E]) -> Enum:
    return Enum(
        kind,
        native_enum=False,  # VARCHAR + a named check: a new value is a plain migration
        create_constraint=False,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        length=20,
    )


# Routes hold no personal data: every field by value.
ROUTE_AUDIT = AuditedEntity(
    type="route",
    fields=(
        "code",
        "corridor_id",
        "route_type",
        "school_name",
        "company_id",
        "morning_time",
        "return_time",
        "seats",
        "launch_minimum",
        "price_round_trip",
        "price_one_way",
        "price_saturday",
        "status",
        "copied_from_route_id",
        "deleted_at",
    ),
    visible=frozenset(
        {
            "code",
            "corridor_id",
            "route_type",
            "school_name",
            "company_id",
            "morning_time",
            "return_time",
            "seats",
            "launch_minimum",
            "price_round_trip",
            "price_one_way",
            "price_saturday",
            "status",
            "copied_from_route_id",
            "deleted_at",
        }
    ),
)


class Route(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "routes"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9-]{0,18}[A-Z0-9]$'", name="code"),
        CheckConstraint("route_type IN ('public', 'student', 'corporate')", name="route_type"),
        CheckConstraint("status IN ('draft', 'forming', 'active', 'paused')", name="status"),
        CheckConstraint(
            "(route_type = 'student') = (school_name IS NOT NULL)"
            " AND (school_name IS NULL"
            " OR (school_name = btrim(school_name) AND school_name <> ''))",
            name="school",
        ),
        CheckConstraint("(route_type = 'corporate') = (company_id IS NOT NULL)", name="company"),
        CheckConstraint("morning_time < return_time", name="times"),
        CheckConstraint(f"seats BETWEEN 1 AND {MAX_SEATS}", name="seats"),
        CheckConstraint(
            "(route_type = 'corporate' AND launch_minimum IS NULL)"
            " OR (route_type <> 'corporate' AND launch_minimum BETWEEN 1 AND seats)",
            name="launch_minimum",
        ),
        CheckConstraint(
            "(route_type <> 'corporate' OR (price_round_trip IS NULL AND price_one_way IS NULL"
            " AND price_saturday IS NULL))"
            " AND (route_type = 'public' OR price_saturday IS NULL)"
            f" AND (price_round_trip IS NULL OR price_round_trip BETWEEN 1 AND {MAX_PRICE})"
            f" AND (price_one_way IS NULL OR price_one_way BETWEEN 1 AND {MAX_PRICE})"
            f" AND (price_saturday IS NULL OR price_saturday BETWEEN 1 AND {MAX_PRICE})",
            name="prices",
        ),
        CheckConstraint(
            "route_type <> 'corporate' OR status <> 'forming'", name="corporate_status"
        ),
        Index(ROUTE_CODE_UNIQUE, "code", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_routes_corridor_id", "corridor_id"),  # filter, "corridor in use"
        Index("ix_routes_company_id", "company_id"),  # filter, "company in use"
        Index("ix_routes_created_at_id", "created_at", "id"),  # the list's order
    )

    code: Mapped[str] = mapped_column(String(ROUTE_CODE_MAX_LENGTH))
    corridor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gize.corridors.id", ondelete="RESTRICT")
    )
    route_type: Mapped[RouteType] = mapped_column(_string_enum(RouteType))
    school_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.companies.id", ondelete="RESTRICT")
    )
    morning_time: Mapped[time] = mapped_column(Time)
    return_time: Mapped[time] = mapped_column(Time)
    seats: Mapped[int] = mapped_column(SmallInteger)
    launch_minimum: Mapped[int | None] = mapped_column(SmallInteger)
    price_round_trip: Mapped[int | None] = mapped_column(Integer)
    price_one_way: Mapped[int | None] = mapped_column(Integer)
    price_saturday: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[RouteStatus] = mapped_column(
        _string_enum(RouteStatus),
        default=RouteStatus.DRAFT,
        server_default=RouteStatus.DRAFT.value,
    )
    copied_from_route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.routes.id", ondelete="RESTRICT")
    )
    # Loaded with the route where needed (repository); never lazily, which async can't do.
    corridor: Mapped[Corridor] = relationship(lazy="raise")
    company: Mapped[Company | None] = relationship(lazy="raise")


class RouteAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_assignments"
    __table_args__ = (
        CheckConstraint("status IN ('reserved', 'active', 'ended')", name="status"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
        Index("ix_route_assignments_route_id_start_date", "route_id", "start_date"),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gize.routes.id", ondelete="RESTRICT"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[AssignmentStatus] = mapped_column(_string_enum(AssignmentStatus))
