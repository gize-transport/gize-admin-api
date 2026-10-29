"""Staff accounts (GZ-011).

A staff member's ID is their Supabase user ID: sign-in proves who they are, this table
says whether they are staff, active, and in which role. The role is never taken from the
token. Deactivating a member (`is_active = false`) locks them out on their next request.
"""

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Enum, String, true
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.models import Base, TimestampMixin

FULL_NAME_MAX_LENGTH = 200
EMAIL_MAX_LENGTH = 320  # RFC 5321 path limit


class StaffRole(StrEnum):
    ADMIN = "admin"
    STAFF = "staff"


class Staff(TimestampMixin, Base):
    __tablename__ = "staff"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'staff')", name="role"),
        CheckConstraint("email = lower(btrim(email)) AND position('@' in email) > 1", name="email"),
        CheckConstraint("btrim(full_name) <> ''", name="full_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # the Supabase user ID
    full_name: Mapped[str] = mapped_column(String(FULL_NAME_MAX_LENGTH))
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), unique=True)
    role: Mapped[StaffRole] = mapped_column(
        Enum(
            StaffRole,
            native_enum=False,  # VARCHAR + the named check: adding a role is a plain migration
            create_constraint=False,
            values_callable=lambda roles: [role.value for role in roles],
            validate_strings=True,
            length=20,
        )
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
