"""Audit log (GZ-012).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

Expand only: a new table, with row-level security like every table. Append-only: the app
role keeps INSERT and SELECT only (the schema's default privileges also grant UPDATE and
DELETE, revoked here), so rows can be added and read but never changed or removed by the
app. TRUNCATE was never granted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from gize_api.core.rls import APP_ROLE, disable_row_level_security, enable_row_level_security

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_staff_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "action ~ '^[a-z][a-z_]*\\.[a-z][a-z_]*$'", name=op.f("ck_audit_log_action")
        ),
        sa.CheckConstraint("entity_type ~ '^[a-z][a-z_]*$'", name=op.f("ck_audit_log_entity_type")),
        sa.CheckConstraint(
            "before IS NOT NULL OR after IS NOT NULL", name=op.f("ck_audit_log_change")
        ),
        sa.ForeignKeyConstraint(
            ["actor_staff_id"],
            ["gize.staff.id"],
            name=op.f("fk_audit_log_actor_staff_id_staff"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        schema="gize",
    )
    op.create_index("ix_audit_log_created_at_id", "audit_log", ["created_at", "id"], schema="gize")
    op.create_index(
        "ix_audit_log_entity_type_entity_id_created_at_id",
        "audit_log",
        ["entity_type", "entity_id", "created_at", "id"],
        schema="gize",
    )
    op.create_index(
        "ix_audit_log_actor_staff_id_created_at_id",
        "audit_log",
        ["actor_staff_id", "created_at", "id"],
        schema="gize",
    )
    enable_row_level_security(op, "audit_log")
    op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON "gize"."audit_log" FROM "{APP_ROLE}"')


def downgrade() -> None:
    disable_row_level_security(op, "audit_log")
    op.drop_index(
        "ix_audit_log_actor_staff_id_created_at_id", table_name="audit_log", schema="gize"
    )
    op.drop_index(
        "ix_audit_log_entity_type_entity_id_created_at_id", table_name="audit_log", schema="gize"
    )
    op.drop_index("ix_audit_log_created_at_id", table_name="audit_log", schema="gize")
    op.drop_table("audit_log", schema="gize")
