"""Routes and the seats riders hold (GZ-021).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30

Expand only: two new tables, with row-level security like every table.
- `routes`: type fixed at creation; school for student routes, company for corporate ones;
  prices (set by GZ-022) only on public and student routes, Saturday only on public; status
  `draft` until GZ-024 moves it. Codes unique among live routes.
- `route_assignments`: a seat held from `start_date` to `end_date`. GZ-023 adds the pickup
  point, M3 the rider.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[datetime]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("corridor_id", sa.Uuid(), nullable=False),
        sa.Column("route_type", sa.String(length=20), nullable=False),
        sa.Column("school_name", sa.String(length=100), nullable=True),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("morning_time", sa.Time(), nullable=False),
        sa.Column("return_time", sa.Time(), nullable=False),
        sa.Column("seats", sa.SmallInteger(), nullable=False),
        sa.Column("launch_minimum", sa.SmallInteger(), nullable=True),
        sa.Column("price_round_trip", sa.Integer(), nullable=True),
        sa.Column("price_one_way", sa.Integer(), nullable=True),
        sa.Column("price_saturday", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("copied_from_route_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "code ~ '^[A-Z0-9][A-Z0-9-]{0,18}[A-Z0-9]$'", name=op.f("ck_routes_code")
        ),
        sa.CheckConstraint(
            "route_type IN ('public', 'student', 'corporate')", name=op.f("ck_routes_route_type")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'forming', 'active', 'paused')", name=op.f("ck_routes_status")
        ),
        sa.CheckConstraint(
            "(route_type = 'student') = (school_name IS NOT NULL)"
            " AND (school_name IS NULL"
            " OR (school_name = btrim(school_name) AND school_name <> ''))",
            name=op.f("ck_routes_school"),
        ),
        sa.CheckConstraint(
            "(route_type = 'corporate') = (company_id IS NOT NULL)", name=op.f("ck_routes_company")
        ),
        sa.CheckConstraint("morning_time < return_time", name=op.f("ck_routes_times")),
        sa.CheckConstraint("seats BETWEEN 1 AND 60", name=op.f("ck_routes_seats")),
        sa.CheckConstraint(
            "(route_type = 'corporate' AND launch_minimum IS NULL)"
            " OR (route_type <> 'corporate' AND launch_minimum IS NOT NULL"
            " AND launch_minimum BETWEEN 1 AND seats)",
            name=op.f("ck_routes_launch_minimum"),
        ),
        sa.CheckConstraint(
            "(route_type <> 'corporate' OR (price_round_trip IS NULL AND price_one_way IS NULL"
            " AND price_saturday IS NULL))"
            " AND (route_type = 'public' OR price_saturday IS NULL)"
            " AND (price_round_trip IS NULL OR price_round_trip BETWEEN 1 AND 100000)"
            " AND (price_one_way IS NULL OR price_one_way BETWEEN 1 AND 100000)"
            " AND (price_saturday IS NULL OR price_saturday BETWEEN 1 AND 100000)",
            name=op.f("ck_routes_prices"),
        ),
        sa.CheckConstraint(
            "route_type <> 'corporate' OR status <> 'forming'",
            name=op.f("ck_routes_corporate_status"),
        ),
        sa.ForeignKeyConstraint(
            ["corridor_id"],
            ["gize.corridors.id"],
            name=op.f("fk_routes_corridor_id_corridors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["gize.companies.id"],
            name=op.f("fk_routes_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["copied_from_route_id"],
            ["gize.routes.id"],
            name=op.f("fk_routes_copied_from_route_id_routes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routes")),
        schema="gize",
    )
    op.create_index(
        "uq_routes_code",
        "routes",
        ["code"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_routes_corridor_id", "routes", ["corridor_id"], schema="gize")
    op.create_index("ix_routes_company_id", "routes", ["company_id"], schema="gize")
    op.create_index("ix_routes_created_at_id", "routes", ["created_at", "id"], schema="gize")
    enable_row_level_security(op, "routes")

    op.create_table(
        "route_assignments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('reserved', 'active', 'ended')", name=op.f("ck_route_assignments_status")
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date", name=op.f("ck_route_assignments_dates")
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["gize.routes.id"],
            name=op.f("fk_route_assignments_route_id_routes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_assignments")),
        schema="gize",
    )
    op.create_index(
        "ix_route_assignments_route_id_start_date",
        "route_assignments",
        ["route_id", "start_date"],
        schema="gize",
    )
    enable_row_level_security(op, "route_assignments")


def downgrade() -> None:
    disable_row_level_security(op, "route_assignments")
    op.drop_index(
        "ix_route_assignments_route_id_start_date", table_name="route_assignments", schema="gize"
    )
    op.drop_table("route_assignments", schema="gize")
    disable_row_level_security(op, "routes")
    op.drop_index("ix_routes_created_at_id", table_name="routes", schema="gize")
    op.drop_index("ix_routes_company_id", table_name="routes", schema="gize")
    op.drop_index("ix_routes_corridor_id", table_name="routes", schema="gize")
    op.drop_index("uq_routes_code", table_name="routes", schema="gize")
    op.drop_table("routes", schema="gize")
