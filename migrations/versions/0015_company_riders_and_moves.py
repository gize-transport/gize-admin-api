"""Company riders and moves between routes (GZ-035).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04

Expand only: nullable columns, new checks every existing row passes (no company rider
exists yet), and a narrower seat index.
- `riders.company_id`: a company rider's employer; checks `ck_riders_company` and
  `ck_riders_corporate`; one live rider per phone per company (`uq_riders_company_phone`).
- `route_assignments.skip_reason`: why staff placed a rider ahead of open requests.
- `uq_route_assignments_rider_id_held` (one non-ended seat per rider) becomes
  `uq_route_assignments_rider_id_ongoing` (one open-ended seat): a move keeps the old
  seat to the day before the new one starts, so a rider may hold two seats that follow
  each other. The service keeps them apart under the rider's lock.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("riders", sa.Column("company_id", sa.Uuid(), nullable=True), schema="gize")
    op.create_foreign_key(
        op.f("fk_riders_company_id_companies"),
        "riders",
        "companies",
        ["company_id"],
        ["id"],
        source_schema="gize",
        referent_schema="gize",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_riders_company"),
        "riders",
        "(rider_type = 'corporate') = (company_id IS NOT NULL)",
        schema="gize",
    )
    op.create_check_constraint(
        op.f("ck_riders_corporate"),
        "riders",
        "rider_type <> 'corporate' OR (phone IS NOT NULL AND home_area_id IS NOT NULL"
        " AND destination_area_id IS NULL AND plan IS NULL AND NOT saturday_addon)",
        schema="gize",
    )
    op.create_index(
        "uq_riders_company_phone",
        "riders",
        ["company_id", "phone"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("rider_type = 'corporate' AND deleted_at IS NULL"),
    )
    op.create_index("ix_riders_company_id", "riders", ["company_id"], schema="gize")
    op.add_column(
        "route_assignments",
        sa.Column("skip_reason", sa.String(length=100), nullable=True),
        schema="gize",
    )
    op.create_index(
        "uq_route_assignments_rider_id_ongoing",
        "route_assignments",
        ["rider_id"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("status <> 'ended' AND end_date IS NULL"),
    )
    op.drop_index(
        "uq_route_assignments_rider_id_held", table_name="route_assignments", schema="gize"
    )


def downgrade() -> None:
    # The old index counts every seat not `ended`, and a seat that ran to its end date
    # keeps its status: mark those ended first (they hold nothing any more). A rider
    # holding two seats still to come or running (a move in progress) makes this fail:
    # end one of them first.
    op.execute(
        "UPDATE gize.route_assignments SET status = 'ended'"
        " WHERE status <> 'ended' AND end_date < current_date"
    )
    op.create_index(
        "uq_route_assignments_rider_id_held",
        "route_assignments",
        ["rider_id"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("status <> 'ended'"),
    )
    op.drop_index(
        "uq_route_assignments_rider_id_ongoing", table_name="route_assignments", schema="gize"
    )
    op.drop_column("route_assignments", "skip_reason", schema="gize")
    op.drop_index("ix_riders_company_id", table_name="riders", schema="gize")
    op.drop_index("uq_riders_company_phone", table_name="riders", schema="gize")
    op.drop_constraint(op.f("ck_riders_corporate"), "riders", type_="check", schema="gize")
    op.drop_constraint(op.f("ck_riders_company"), "riders", type_="check", schema="gize")
    op.drop_constraint(
        op.f("fk_riders_company_id_companies"), "riders", type_="foreignkey", schema="gize"
    )
    op.drop_column("riders", "company_id", schema="gize")
