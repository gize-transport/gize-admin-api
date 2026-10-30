"""Corridors (GZ-020).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30

Expand only: a new table, with row-level security like every table. Each pair of areas
once among live corridors; areas are referred to, never deleted under a corridor
(RESTRICT; deleting an area is a soft delete the service refuses while it is in use).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corridors",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("origin_area_id", sa.Uuid(), nullable=False),
        sa.Column("destination_area_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("origin_area_id <> destination_area_id", name=op.f("ck_corridors_ends")),
        sa.ForeignKeyConstraint(
            ["origin_area_id"],
            ["gize.areas.id"],
            name=op.f("fk_corridors_origin_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_area_id"],
            ["gize.areas.id"],
            name=op.f("fk_corridors_destination_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corridors")),
        schema="gize",
    )
    op.create_index(
        "uq_corridors_areas",
        "corridors",
        ["origin_area_id", "destination_area_id"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_corridors_destination_area_id", "corridors", ["destination_area_id"], schema="gize"
    )
    op.create_index("ix_corridors_created_at_id", "corridors", ["created_at", "id"], schema="gize")
    enable_row_level_security(op, "corridors")


def downgrade() -> None:
    disable_row_level_security(op, "corridors")
    op.drop_index("ix_corridors_created_at_id", table_name="corridors", schema="gize")
    op.drop_index("ix_corridors_destination_area_id", table_name="corridors", schema="gize")
    op.drop_index("uq_corridors_areas", table_name="corridors", schema="gize")
    op.drop_table("corridors", schema="gize")
