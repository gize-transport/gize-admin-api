"""Audit log (GZ-012).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

Expand only: a new table, with row-level security like every table. Append-only, enforced
by the database:
- the app role may SELECT, and INSERT only the content columns: `id` and `created_at`
  always come from the database, so an entry cannot be backdated;
- UPDATE and DELETE (from the schema's default privileges) are revoked, and restrictive
  policies refuse them too, should they ever be granted again. TRUNCATE was never granted.

The downgrade refuses while the table has rows: it would destroy the history. Back it up
and empty it as the owner first, deliberately.
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

INSERTABLE = (
    "actor_staff_id",
    "action",
    "entity_type",
    "entity_id",
    "before",
    "after",
    "request_id",
)


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
    table = '"gize"."audit_log"'
    op.execute(f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM "{APP_ROLE}"')
    op.execute(f'GRANT INSERT ({", ".join(INSERTABLE)}) ON {table} TO "{APP_ROLE}"')
    for command in ("UPDATE", "DELETE"):
        op.execute(
            f'CREATE POLICY "gize_app_no_{command.lower()}" ON {table} AS RESTRICTIVE'
            f' FOR {command} TO "{APP_ROLE}" USING (false)'
        )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM gize.audit_log) THEN"
        " RAISE EXCEPTION 'gize.audit_log has rows: back up and empty it first';"
        " END IF; END $$"
    )
    disable_row_level_security(op, "audit_log")
    op.drop_index(
        "ix_audit_log_actor_staff_id_created_at_id", table_name="audit_log", schema="gize"
    )
    op.drop_index(
        "ix_audit_log_entity_type_entity_id_created_at_id", table_name="audit_log", schema="gize"
    )
    op.drop_index("ix_audit_log_created_at_id", table_name="audit_log", schema="gize")
    op.drop_table("audit_log", schema="gize")
