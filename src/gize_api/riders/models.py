"""Riders (GZ-032): the people who ride.

Adults sign themselves up from the public app (GZ-032) or are recorded by staff (GZ-034):
name, phone, home and destination areas, plan and Saturday add-on, the subscription they
asked for. Company riders (GZ-035) are recorded by staff for their company: name, phone and
where they board (`home_area_id`); one live rider per phone per company. Students (GZ-033)
are deferred.

One live adult rider per phone (`uq_riders_adult_phone`). The seat a rider holds is a
`route_assignments` row naming them; a seat asked for, a `route_requests` row. Names and
phones are personal data: never logged.
"""

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gize_api.areas.models import Area
from gize_api.companies.models import Company
from gize_api.core.audit import AuditedEntity
from gize_api.core.contact import MOBILE_E164_LENGTH
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH

RIDER_ADULT_PHONE_UNIQUE = "uq_riders_adult_phone"
RIDER_COMPANY_PHONE_UNIQUE = "uq_riders_company_phone"

# Names and phones are personal data: the log shows that they changed, never their values.
RIDER_AUDIT = AuditedEntity(
    type="rider",
    fields=(
        "rider_type",
        "full_name",
        "phone",
        "company_id",
        "home_area_id",
        "destination_area_id",
        "plan",
        "saturday_addon",
        "source",
        "deleted_at",
    ),
    visible=frozenset(
        {
            "rider_type",
            "company_id",
            "home_area_id",
            "destination_area_id",
            "plan",
            "saturday_addon",
            "source",
            "deleted_at",
        }
    ),
)


class RiderType(StrEnum):
    ADULT = "adult"
    STUDENT = "student"
    CORPORATE = "corporate"


class RiderPlan(StrEnum):
    ROUND_TRIP = "round_trip"
    MORNING = "morning"  # one way: A to B
    EVENING = "evening"  # one way: B to A


class RiderSource(StrEnum):
    APP = "app"
    STAFF = "staff"


def _string_enum[E: StrEnum](kind: type[E]) -> Enum:
    return Enum(
        kind,
        native_enum=False,  # VARCHAR + a named check: a new value is a plain migration
        create_constraint=False,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        length=20,
    )


class Rider(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "riders"
    __table_args__ = (
        CheckConstraint("rider_type IN ('adult', 'student', 'corporate')", name="rider_type"),
        CheckConstraint("plan IN ('round_trip', 'morning', 'evening')", name="plan"),
        CheckConstraint("source IN ('app', 'staff')", name="source"),
        CheckConstraint("phone ~ '^\\+251[79][0-9]{8}$'", name="phone"),
        CheckConstraint("full_name = btrim(full_name) AND full_name <> ''", name="full_name"),
        # Adults: everything a subscription needs. Other types add their rules later.
        CheckConstraint(
            "rider_type <> 'adult' OR (phone IS NOT NULL AND home_area_id IS NOT NULL"
            " AND destination_area_id IS NOT NULL AND plan IS NOT NULL)",
            name="adult",
        ),
        CheckConstraint("home_area_id <> destination_area_id", name="areas"),
        # Company riders (GZ-035): their company, a phone and where they board; no
        # destination (the route's B), plan or Saturday add-on (the company is billed).
        CheckConstraint("(rider_type = 'corporate') = (company_id IS NOT NULL)", name="company"),
        CheckConstraint(
            "rider_type <> 'corporate' OR (phone IS NOT NULL AND home_area_id IS NOT NULL"
            " AND destination_area_id IS NULL AND plan IS NULL AND NOT saturday_addon)",
            name="corporate",
        ),
        Index(
            RIDER_ADULT_PHONE_UNIQUE,
            "phone",
            unique=True,
            postgresql_where=text("rider_type = 'adult' AND deleted_at IS NULL"),
        ),
        Index(
            RIDER_COMPANY_PHONE_UNIQUE,
            "company_id",
            "phone",
            unique=True,
            postgresql_where=text("rider_type = 'corporate' AND deleted_at IS NULL"),
        ),
        Index("ix_riders_home_area_id", "home_area_id"),  # "area in use"
        Index("ix_riders_destination_area_id", "destination_area_id"),
        Index("ix_riders_created_at_id", "created_at", "id"),  # GZ-034's list
        Index("ix_riders_company_id", "company_id"),  # "company in use"
    )

    rider_type: Mapped[RiderType] = mapped_column(_string_enum(RiderType))
    full_name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    phone: Mapped[str | None] = mapped_column(String(MOBILE_E164_LENGTH))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.companies.id", ondelete="RESTRICT")
    )
    home_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    destination_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    plan: Mapped[RiderPlan | None] = mapped_column(_string_enum(RiderPlan))
    saturday_addon: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source: Mapped[RiderSource] = mapped_column(_string_enum(RiderSource))
    # Loaded with the rider (repository); never lazily, which async can't do.
    home_area: Mapped[Area | None] = relationship(foreign_keys=[home_area_id], lazy="raise")
    destination_area: Mapped[Area | None] = relationship(
        foreign_keys=[destination_area_id], lazy="raise"
    )
    company: Mapped[Company | None] = relationship(lazy="raise")
