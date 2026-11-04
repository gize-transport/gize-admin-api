"""Phone verifications (GZ-031).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04

Expand only: one new table, with row-level security like every table.
- `phone_verifications`: one row per code sent; the code kept only as a keyed hash, with its
  expiry, attempts and when it was confirmed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phone_verifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone", sa.String(length=13), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_phone_verifications_phone")
        ),
        sa.CheckConstraint(
            "code_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_phone_verifications_code_hash")
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 5", name=op.f("ck_phone_verifications_attempts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_phone_verifications")),
        schema="gize",
    )
    op.create_index(
        "ix_phone_verifications_created_at", "phone_verifications", ["created_at"], schema="gize"
    )
    enable_row_level_security(op, "phone_verifications")


def downgrade() -> None:
    disable_row_level_security(op, "phone_verifications")
    op.drop_index(
        "ix_phone_verifications_created_at", table_name="phone_verifications", schema="gize"
    )
    op.drop_table("phone_verifications", schema="gize")
