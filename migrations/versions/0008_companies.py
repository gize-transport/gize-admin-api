"""Companies (GZ-029).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30

Expand only: a new table, with row-level security like every table. Names unique ignoring
case among live companies; the contact phone is stored E.164 (+2519…/+2517…).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("contact_name", sa.String(length=100), nullable=False),
        sa.Column("contact_phone", sa.String(length=13), nullable=False),
        sa.Column("contact_email", sa.String(length=320), nullable=False),
        sa.Column("billing", sa.String(length=20), server_default="postpaid", nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("name = btrim(name) AND name <> ''", name=op.f("ck_companies_name")),
        sa.CheckConstraint(
            "contact_name = btrim(contact_name) AND contact_name <> ''",
            name=op.f("ck_companies_contact_name"),
        ),
        sa.CheckConstraint(
            "contact_phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_companies_contact_phone")
        ),
        sa.CheckConstraint(
            "contact_email = lower(btrim(contact_email)) AND position('@' in contact_email) > 1",
            name=op.f("ck_companies_contact_email"),
        ),
        sa.CheckConstraint("billing IN ('postpaid')", name=op.f("ck_companies_billing")),
        sa.CheckConstraint(
            "notes IS NULL OR (notes = btrim(notes) AND notes <> '')",
            name=op.f("ck_companies_notes"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        schema="gize",
    )
    op.create_index(
        "uq_companies_name",
        "companies",
        [sa.text("lower(name)")],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_companies_created_at_id", "companies", ["created_at", "id"], schema="gize")
    enable_row_level_security(op, "companies")


def downgrade() -> None:
    disable_row_level_security(op, "companies")
    op.drop_index("ix_companies_created_at_id", table_name="companies", schema="gize")
    op.drop_index("uq_companies_name", table_name="companies", schema="gize")
    op.drop_table("companies", schema="gize")
