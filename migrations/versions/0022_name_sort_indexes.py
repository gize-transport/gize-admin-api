"""Indexes for lists sorted by name (GZ-082).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-20

Expand only: drivers (live) and staff by name ignoring case (`lower(full_name)`, then
id), so a sorted page reads only its rows. Companies and areas use their unique
`lower(name)` indexes; corridors, a small list, sort on their areas' names unindexed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_drivers_lower_full_name_id",
        "drivers",
        [sa.text("lower(full_name)"), "id"],
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_staff_lower_full_name_id", "staff", [sa.text("lower(full_name)"), "id"], schema="gize"
    )


def downgrade() -> None:
    op.drop_index("ix_staff_lower_full_name_id", table_name="staff", schema="gize")
    op.drop_index("ix_drivers_lower_full_name_id", table_name="drivers", schema="gize")
