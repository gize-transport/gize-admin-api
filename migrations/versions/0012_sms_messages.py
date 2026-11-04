"""SMS messages (GZ-030).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-04

Expand only: one new table, with row-level security like every table.
- `sms_messages`: one row per SMS the app tries to send: the event's de-duplication key
  (`uq_sms_messages_dedup_key`), purpose, phone and outcome. Never the text.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sms_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dedup_key", sa.String(length=100), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("to_phone", sa.String(length=13), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="sending", nullable=False),
        sa.Column("error", sa.String(length=20), nullable=True),
        sa.Column("provider_message_id", sa.String(length=100), nullable=True),
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
            "dedup_key = btrim(dedup_key) AND dedup_key <> '' AND length(dedup_key) <= 100",
            name=op.f("ck_sms_messages_dedup_key"),
        ),
        sa.CheckConstraint(
            "purpose IN ('phone_verification')", name=op.f("ck_sms_messages_purpose")
        ),
        sa.CheckConstraint(
            "to_phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_sms_messages_to_phone")
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed')"
            " AND (status = 'failed') = (error IS NOT NULL)"
            " AND (error IS NULL OR error IN ('unavailable', 'rejected', 'timeout', 'error'))",
            name=op.f("ck_sms_messages_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_messages")),
        schema="gize",
    )
    op.create_index(
        "uq_sms_messages_dedup_key", "sms_messages", ["dedup_key"], unique=True, schema="gize"
    )
    op.create_index("ix_sms_messages_created_at", "sms_messages", ["created_at"], schema="gize")
    enable_row_level_security(op, "sms_messages")


def downgrade() -> None:
    disable_row_level_security(op, "sms_messages")
    op.drop_index("ix_sms_messages_created_at", table_name="sms_messages", schema="gize")
    op.drop_index("uq_sms_messages_dedup_key", table_name="sms_messages", schema="gize")
    op.drop_table("sms_messages", schema="gize")
