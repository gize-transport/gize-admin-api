"""Routes and the seats riders hold on them (GZ-021).

A route is one van going from A to B (its corridor): the morning run leaves A and arrives
at B at set times, the evening run back (local Addis Ababa), passing the areas it serves.
Its type is fixed at creation (owner decision):
- `public` and `student` routes are for prepaid individuals: they have prices (GZ-022) and
  a launch minimum, and collect reservations while `forming` (GZ-024);
- `corporate` routes belong to one company: no prices on the route, no minimum, never
  `forming`.

A `RouteAssignment` holds a seat on every day from `start_date` to `end_date` (open-ended
while `end_date` is null), unless `ended`. Seats are counted per day, so company riders
swapping mid-month take one seat. The rider holding it (GZ-032) keeps their plan and
Saturday add-on on their own row. Assignments are never deleted: they end.
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

from gize_api.areas.models import Area
from gize_api.companies.models import Company
from gize_api.core.audit import AuditedEntity
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH
from gize_api.corridors.models import Corridor
from gize_api.drivers.models import Driver

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
        "morning_arrival_time",
        "return_time",
        "return_arrival_time",
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
            "morning_arrival_time",
            "return_time",
            "return_arrival_time",
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
        # Arrival times (GZ-023): required by the API; nullable here until every deployed
        # row has them (expand, then contract).
        CheckConstraint(
            "(morning_arrival_time IS NULL) = (return_arrival_time IS NULL)"
            " AND (morning_arrival_time IS NULL OR (morning_time < morning_arrival_time"
            " AND morning_arrival_time < return_time AND return_time < return_arrival_time))",
            name="arrival_times",
        ),
        CheckConstraint(f"seats BETWEEN 1 AND {MAX_SEATS}", name="seats"),
        CheckConstraint(
            "(route_type = 'corporate' AND launch_minimum IS NULL)"
            " OR (route_type <> 'corporate' AND launch_minimum IS NOT NULL"
            " AND launch_minimum BETWEEN 1 AND seats)",
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
        Index("ix_routes_driver_id", "driver_id"),  # a driver's routes (GZ-053)
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
    morning_time: Mapped[time] = mapped_column(Time)  # leaves A
    morning_arrival_time: Mapped[time | None] = mapped_column(Time)  # at B
    return_time: Mapped[time] = mapped_column(Time)  # leaves B
    return_arrival_time: Mapped[time | None] = mapped_column(Time)  # at A
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
    # Who drives it (GZ-053): an active driver whose other routes' runs don't overlap.
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.drivers.id", ondelete="RESTRICT")
    )
    # Loaded with the route where needed (repository); never lazily, which async can't do.
    corridor: Mapped[Corridor] = relationship(lazy="raise")
    company: Mapped[Company | None] = relationship(lazy="raise")
    driver: Mapped[Driver | None] = relationship(lazy="raise")
    route_areas: Mapped[list["RouteArea"]] = relationship(
        lazy="raise", order_by="RouteArea.position"
    )


# A launch (GZ-024) also records the day riders start and how many: their seats aren't
# audited one by one.
ROUTE_LAUNCH_AUDIT = AuditedEntity(
    type="route",
    fields=("status", "launch_date", "riders_started", "lapsed"),
    visible=frozenset({"status", "launch_date", "riders_started", "lapsed"}),
)

MAX_SERVED_AREAS = 20

# The served areas are recorded as one ordered list on the route.
ROUTE_DRIVER_AUDIT = AuditedEntity(
    type="route", fields=("driver_id",), visible=frozenset({"driver_id"})
)
ROUTE_AREAS_AUDIT = AuditedEntity(
    type="route", fields=("served_area_ids",), visible=frozenset({"served_area_ids"})
)


# Seats staff give, move and end (GZ-035). The skip reason is free text: redacted.
ROUTE_ASSIGNMENT_AUDIT = AuditedEntity(
    type="route_assignment",
    fields=("route_id", "rider_id", "start_date", "end_date", "status", "skip_reason"),
    visible=frozenset({"route_id", "rider_id", "start_date", "end_date", "status"}),
)


class RouteArea(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An area a route passes between A and B, where riders board or get off (GZ-023): in
    order, no times (owner decision). Replaced as a whole list; A and B are the corridor's."""

    __tablename__ = "route_areas"
    __table_args__ = (
        CheckConstraint("position >= 1", name="position"),
        Index("uq_route_areas_route_id_position", "route_id", "position", unique=True),
        Index("uq_route_areas_route_id_area_id", "route_id", "area_id", unique=True),
        Index("ix_route_areas_area_id", "area_id"),  # "area in use"
    )

    route_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gize.routes.id", ondelete="RESTRICT"))
    area_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gize.areas.id", ondelete="RESTRICT"))
    position: Mapped[int] = mapped_column(SmallInteger)
    area: Mapped[Area] = relationship(lazy="raise")


class RouteAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "route_assignments"
    __table_args__ = (
        CheckConstraint("status IN ('reserved', 'active', 'ended')", name="status"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
        Index("ix_route_assignments_route_id_start_date", "route_id", "start_date"),
        # Seats not ended, by status and end (the overview's rider counts: GZ-064, GZ-080).
        Index(
            "ix_route_assignments_held",
            "status",
            "end_date",
            postgresql_where=text("status <> 'ended'"),
        ),
        # One ongoing (open-ended) seat per rider (GZ-035): a move keeps the old seat to
        # the day before the new one starts; the service keeps a rider's seats apart.
        Index(
            "uq_route_assignments_rider_id_ongoing",
            "rider_id",
            unique=True,
            postgresql_where=text("status <> 'ended' AND end_date IS NULL"),
        ),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gize.routes.id", ondelete="RESTRICT"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[AssignmentStatus] = mapped_column(_string_enum(AssignmentStatus))
    # Who holds the seat (GZ-032); null only on seats made before riders existed.
    rider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.riders.id", ondelete="RESTRICT")
    )
    # Why staff placed this rider ahead of open requests for the route (GZ-035).
    skip_reason: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    # The launch day, on a reservation a launch started (GZ-041): its rider gets the SMS.
    launched_on: Mapped[date | None] = mapped_column(Date)
