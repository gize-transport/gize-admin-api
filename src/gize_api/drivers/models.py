"""Drivers (GZ-050): who drives Gize's vans.

Basic information only for the MVP (owner decision, 2026-11-05): full name, phone, address
and the plate number of the van they drive. Documents, inspection and approval come later
(GZ-051, GZ-052, deferred). One live driver per phone and per plate. The name, phone and
address are personal data: redacted in the audit log, never in a URL or a log.
"""

from sqlalchemy import Boolean, CheckConstraint, Index, String, text, true
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.audit import AuditedEntity
from gize_api.core.contact import MOBILE_E164_LENGTH
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH

DRIVER_PHONE_UNIQUE = "uq_drivers_phone"
DRIVER_PLATE_UNIQUE = "uq_drivers_plate"
ADDRESS_MAX_LENGTH = 200
PLATE_MAX_LENGTH = 20
# Capitals, digits, single spaces and dashes, 2 to 20: "AA 3-B12345".
PLATE_PATTERN = r"^[A-Z0-9][A-Z0-9 -]{0,18}[A-Z0-9]$"

DRIVER_AUDIT = AuditedEntity(
    type="driver",
    fields=("full_name", "phone", "address", "plate", "is_active", "deleted_at"),
    visible=frozenset({"plate", "is_active", "deleted_at"}),
)


class Driver(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "drivers"
    __table_args__ = (
        CheckConstraint("full_name = btrim(full_name) AND full_name <> ''", name="full_name"),
        CheckConstraint("phone ~ '^\\+251[79][0-9]{8}$'", name="phone"),
        CheckConstraint("address = btrim(address) AND address <> ''", name="address"),
        CheckConstraint(f"plate ~ '{PLATE_PATTERN}' AND plate NOT LIKE '%  %'", name="plate"),
        Index(
            DRIVER_PHONE_UNIQUE, "phone", unique=True, postgresql_where=text("deleted_at IS NULL")
        ),
        Index(
            DRIVER_PLATE_UNIQUE, "plate", unique=True, postgresql_where=text("deleted_at IS NULL")
        ),
        Index("ix_drivers_created_at_id", "created_at", "id"),  # the list's order
    )

    full_name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    phone: Mapped[str] = mapped_column(String(MOBILE_E164_LENGTH))
    address: Mapped[str] = mapped_column(String(ADDRESS_MAX_LENGTH))
    plate: Mapped[str] = mapped_column(String(PLATE_MAX_LENGTH))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
