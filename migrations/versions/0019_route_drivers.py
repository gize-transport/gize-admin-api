"""A driver for each route (GZ-053).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-05

Expand only: a nullable column. `routes.driver_id`: who drives it (a driver may drive
several routes whose runs don't overlap: checked by the service under the driver's lock).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("driver_id", sa.Uuid(), nullable=True), schema="gize")
    op.create_foreign_key(
        op.f("fk_routes_driver_id_drivers"),
        "routes",
        "drivers",
        ["driver_id"],
        ["id"],
        source_schema="gize",
        referent_schema="gize",
        ondelete="RESTRICT",
    )
    op.create_index("ix_routes_driver_id", "routes", ["driver_id"], schema="gize")


def downgrade() -> None:
    op.drop_index("ix_routes_driver_id", table_name="routes", schema="gize")
    op.drop_constraint(
        op.f("fk_routes_driver_id_drivers"), "routes", type_="foreignkey", schema="gize"
    )
    op.drop_column("routes", "driver_id", schema="gize")
