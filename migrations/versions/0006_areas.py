"""Areas (GZ-026).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30

Expand only: a new table, with row-level security like every table. Names are unique
ignoring case among live (not deleted) areas.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "areas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
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
        sa.CheckConstraint("name = btrim(name) AND name <> ''", name=op.f("ck_areas_name")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_areas")),
        schema="gize",
    )
    op.create_index(
        "uq_areas_name",
        "areas",
        [sa.text("lower(name)")],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_areas_created_at_id", "areas", ["created_at", "id"], schema="gize")
    enable_row_level_security(op, "areas")


def downgrade() -> None:
    disable_row_level_security(op, "areas")
    op.drop_index("ix_areas_created_at_id", table_name="areas", schema="gize")
    op.drop_index("uq_areas_name", table_name="areas", schema="gize")
    op.drop_table("areas", schema="gize")
