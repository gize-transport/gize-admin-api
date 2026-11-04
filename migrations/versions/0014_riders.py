"""Riders (GZ-032).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-04

Expand only: a new table, and nullable columns on existing ones (nothing to backfill).
- `riders`: who rides; one live adult per phone (`uq_riders_adult_phone`); RLS.
- `route_assignments.rider_id`: who holds the seat; one held seat per rider
  (`uq_route_assignments_rider_id_held`). Seats made before riders existed keep none.
- `route_requests.rider_id`: the rider who asked from the public app.
- `phone_verifications.consumed_at`: when a sign-up spent the verification's token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RIDER_INDEXES = (
    ("ix_riders_home_area_id", ["home_area_id"]),
    ("ix_riders_destination_area_id", ["destination_area_id"]),
    ("ix_riders_created_at_id", ["created_at", "id"]),
)


def upgrade() -> None:
    op.create_table(
        "riders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("rider_type", sa.String(length=20), nullable=False),
        sa.Column("full_name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=13), nullable=True),
        sa.Column("home_area_id", sa.Uuid(), nullable=True),
        sa.Column("destination_area_id", sa.Uuid(), nullable=True),
        sa.Column("plan", sa.String(length=20), nullable=True),
        sa.Column("saturday_addon", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
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
        sa.CheckConstraint(
            "rider_type IN ('adult', 'student', 'corporate')", name=op.f("ck_riders_rider_type")
        ),
        sa.CheckConstraint(
            "plan IN ('round_trip', 'morning', 'evening')", name=op.f("ck_riders_plan")
        ),
        sa.CheckConstraint("source IN ('app', 'staff')", name=op.f("ck_riders_source")),
        sa.CheckConstraint("phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_riders_phone")),
        sa.CheckConstraint(
            "full_name = btrim(full_name) AND full_name <> ''", name=op.f("ck_riders_full_name")
        ),
        sa.CheckConstraint(
            "rider_type <> 'adult' OR (phone IS NOT NULL AND home_area_id IS NOT NULL"
            " AND destination_area_id IS NOT NULL AND plan IS NOT NULL)",
            name=op.f("ck_riders_adult"),
        ),
        sa.CheckConstraint("home_area_id <> destination_area_id", name=op.f("ck_riders_areas")),
        sa.ForeignKeyConstraint(
            ["home_area_id"],
            ["gize.areas.id"],
            name=op.f("fk_riders_home_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_area_id"],
            ["gize.areas.id"],
            name=op.f("fk_riders_destination_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_riders")),
        schema="gize",
    )
    op.create_index(
        "uq_riders_adult_phone",
        "riders",
        ["phone"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("rider_type = 'adult' AND deleted_at IS NULL"),
    )
    for name, columns in _RIDER_INDEXES:
        op.create_index(name, "riders", columns, schema="gize")
    enable_row_level_security(op, "riders")

    for table in ("route_assignments", "route_requests"):
        op.add_column(table, sa.Column("rider_id", sa.Uuid(), nullable=True), schema="gize")
        op.create_foreign_key(
            op.f(f"fk_{table}_rider_id_riders"),
            table,
            "riders",
            ["rider_id"],
            ["id"],
            source_schema="gize",
            referent_schema="gize",
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_route_assignments_rider_id_held",
        "route_assignments",
        ["rider_id"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("status <> 'ended'"),
    )
    op.create_index("ix_route_requests_rider_id", "route_requests", ["rider_id"], schema="gize")
    op.add_column(
        "phone_verifications",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        schema="gize",
    )


def downgrade() -> None:
    op.drop_column("phone_verifications", "consumed_at", schema="gize")
    op.drop_index("ix_route_requests_rider_id", table_name="route_requests", schema="gize")
    op.drop_index(
        "uq_route_assignments_rider_id_held", table_name="route_assignments", schema="gize"
    )
    for table in ("route_requests", "route_assignments"):
        op.drop_constraint(
            op.f(f"fk_{table}_rider_id_riders"), table, type_="foreignkey", schema="gize"
        )
        op.drop_column(table, "rider_id", schema="gize")
    disable_row_level_security(op, "riders")
    for name, _ in reversed(_RIDER_INDEXES):
        op.drop_index(name, table_name="riders", schema="gize")
    op.drop_index("uq_riders_adult_phone", table_name="riders", schema="gize")
    op.drop_table("riders", schema="gize")
