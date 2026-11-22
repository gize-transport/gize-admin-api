"""An index for the open seat offers across all routes (GZ-084).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-22

Expand only: open offers by when they run out, then id, so a page of `GET /v1/offers`
reads only its rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_seat_offers_open_expires_at_id",
        "seat_offers",
        ["expires_at", "id"],
        schema="gize",
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_seat_offers_open_expires_at_id", table_name="seat_offers", schema="gize")
