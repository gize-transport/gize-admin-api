"""Route arrival times and the areas a route serves (GZ-023).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31

Expand only. A route is A to B (owner decision): the morning run leaves A and arrives at
B, the evening run back; riders board in the areas it serves on the way.
- `routes.morning_arrival_time` and `return_arrival_time`: nullable here so existing rows
  stay valid (the API requires them); a later contract step makes them NOT NULL once every
  deployed row has them. When set, in order: leave A, arrive at B, leave B, arrive at A.
- `route_areas`: the served areas in order, each once per route.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes", sa.Column("morning_arrival_time", sa.Time(), nullable=True), schema="gize"
    )
    op.add_column(
        "routes", sa.Column("return_arrival_time", sa.Time(), nullable=True), schema="gize"
    )
    op.create_check_constraint(
        op.f("ck_routes_arrival_times"),
        "routes",
        "(morning_arrival_time IS NULL) = (return_arrival_time IS NULL)"
        " AND (morning_arrival_time IS NULL OR (morning_time < morning_arrival_time"
        " AND morning_arrival_time < return_time AND return_time < return_arrival_time))",
        schema="gize",
    )
    op.create_table(
        "route_areas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
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
        sa.CheckConstraint("position >= 1", name=op.f("ck_route_areas_position")),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["gize.routes.id"],
            name=op.f("fk_route_areas_route_id_routes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["gize.areas.id"],
            name=op.f("fk_route_areas_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_areas")),
        schema="gize",
    )
    op.create_index(
        "uq_route_areas_route_id_position",
        "route_areas",
        ["route_id", "position"],
        unique=True,
        schema="gize",
    )
    op.create_index(
        "uq_route_areas_route_id_area_id",
        "route_areas",
        ["route_id", "area_id"],
        unique=True,
        schema="gize",
    )
    op.create_index("ix_route_areas_area_id", "route_areas", ["area_id"], schema="gize")
    enable_row_level_security(op, "route_areas")


def downgrade() -> None:
    disable_row_level_security(op, "route_areas")
    op.drop_index("ix_route_areas_area_id", table_name="route_areas", schema="gize")
    op.drop_index("uq_route_areas_route_id_area_id", table_name="route_areas", schema="gize")
    op.drop_index("uq_route_areas_route_id_position", table_name="route_areas", schema="gize")
    op.drop_table("route_areas", schema="gize")
    op.drop_constraint(op.f("ck_routes_arrival_times"), "routes", schema="gize", type_="check")
    op.drop_column("routes", "return_arrival_time", schema="gize")
    op.drop_column("routes", "morning_arrival_time", schema="gize")
