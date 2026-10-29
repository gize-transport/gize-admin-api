"""Idempotency keys (GZ-008).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28

Expand only: a new table, with row-level security like every table. Rows live 24 h and are
deleted by the worker's cleanup job (index on expires_at).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.String(length=100), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key", name=op.f("pk_idempotency_keys")),
        schema="gize",
    )
    op.create_index(
        op.f("ix_idempotency_keys_expires_at"),
        "idempotency_keys",
        ["expires_at"],
        unique=False,
        schema="gize",
    )
    enable_row_level_security(op, "idempotency_keys")


def downgrade() -> None:
    disable_row_level_security(op, "idempotency_keys")
    op.drop_index(
        op.f("ix_idempotency_keys_expires_at"), table_name="idempotency_keys", schema="gize"
    )
    op.drop_table("idempotency_keys", schema="gize")
