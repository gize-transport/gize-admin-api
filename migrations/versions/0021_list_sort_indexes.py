"""Indexes for sorted lists (GZ-081).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-20

Expand only: riders by name ignoring case (`lower(full_name)`, then id) and routes by
morning departure (then id), both over live rows, so a sorted page reads only its rows.
Routes by code use `uq_routes_code`; requests newest first, `ix_route_requests_created_at_id`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_riders_lower_full_name_id",
        "riders",
        [sa.text("lower(full_name)"), "id"],
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_routes_morning_time_id",
        "routes",
        ["morning_time", "id"],
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_routes_morning_time_id", table_name="routes", schema="gize")
    op.drop_index("ix_riders_lower_full_name_id", table_name="riders", schema="gize")
