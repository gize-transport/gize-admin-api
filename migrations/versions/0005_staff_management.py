"""Staff management (GZ-013).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29

Expand only:
- `first_signed_in_at`, nullable, set by a member's first request. Only admins who have
  signed in count as the active admin who must remain. Existing rows start empty and are
  filled on their next request.
- the index `GET /v1/staff` pages through, newest first by `(created_at, id)`. A plain
  CREATE INDEX blocks writes to gize.staff while it builds; the table holds a few dozen
  rows, so that is milliseconds (CONCURRENTLY would need a migration outside a transaction
  for no gain).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("first_signed_in_at", sa.DateTime(timezone=True), nullable=True),
        schema="gize",
    )
    op.create_index("ix_staff_created_at_id", "staff", ["created_at", "id"], schema="gize")


def downgrade() -> None:
    op.drop_index("ix_staff_created_at_id", table_name="staff", schema="gize")
    op.drop_column("staff", "first_signed_in_at", schema="gize")
