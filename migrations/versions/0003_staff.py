"""Staff accounts (GZ-011).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29

Expand only: a new table, with row-level security like every table. The ID is the
Supabase user ID; the first admin is created with the GZ-014 command.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staff",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('admin', 'staff')", name=op.f("ck_staff_role")),
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND position('@' in email) > 1",
            name=op.f("ck_staff_email"),
        ),
        sa.CheckConstraint("btrim(full_name) <> ''", name=op.f("ck_staff_full_name")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff")),
        sa.UniqueConstraint("email", name=op.f("uq_staff_email")),
        schema="gize",
    )
    enable_row_level_security(op, "staff")


def downgrade() -> None:
    disable_row_level_security(op, "staff")
    op.drop_table("staff", schema="gize")
