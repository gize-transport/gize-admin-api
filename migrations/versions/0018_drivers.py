"""Drivers (GZ-050).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-05

Expand only: a new table, with row-level security like every table.
- `drivers`: full name, phone, address and plate (owner decision: basic information only);
  one live driver per phone (`uq_drivers_phone`) and per plate (`uq_drivers_plate`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "drivers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("full_name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=13), nullable=False),
        sa.Column("address", sa.String(length=200), nullable=False),
        sa.Column("plate", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "full_name = btrim(full_name) AND full_name <> ''", name=op.f("ck_drivers_full_name")
        ),
        sa.CheckConstraint("phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_drivers_phone")),
        sa.CheckConstraint(
            "address = btrim(address) AND address <> ''", name=op.f("ck_drivers_address")
        ),
        sa.CheckConstraint(
            "plate ~ '^[A-Z0-9][A-Z0-9 -]{0,18}[A-Z0-9]$' AND plate NOT LIKE '%  %'",
            name=op.f("ck_drivers_plate"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drivers")),
        schema="gize",
    )
    for name, column in (("uq_drivers_phone", "phone"), ("uq_drivers_plate", "plate")):
        op.create_index(
            name,
            "drivers",
            [column],
            unique=True,
            schema="gize",
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    op.create_index("ix_drivers_created_at_id", "drivers", ["created_at", "id"], schema="gize")
    enable_row_level_security(op, "drivers")


def downgrade() -> None:
    disable_row_level_security(op, "drivers")
    for name in ("ix_drivers_created_at_id", "uq_drivers_plate", "uq_drivers_phone"):
        op.drop_index(name, table_name="drivers", schema="gize")
    op.drop_table("drivers", schema="gize")
