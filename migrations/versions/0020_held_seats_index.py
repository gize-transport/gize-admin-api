"""An index for the overview's seat counts (GZ-064).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-06

Expand only: `ix_route_assignments_held` (status, end_date) on seats not ended, so riding
and reserved riders are counted without reading every seat ever held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_route_assignments_held",
        "route_assignments",
        ["status", "end_date"],
        schema="gize",
        postgresql_where=sa.text("status <> 'ended'"),
    )


def downgrade() -> None:
    op.drop_index("ix_route_assignments_held", table_name="route_assignments", schema="gize")
