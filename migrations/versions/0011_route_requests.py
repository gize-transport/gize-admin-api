"""Route requests (GZ-027).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31

Expand only: one new table, with row-level security like every table.
- `route_requests`: unmet demand from individual riders, for two areas (listed, or named
  when not listed yet) or for a running route; `new` → `matched` → `fulfilled`, or
  `declined` with a reason. One open request per phone, kind, route or area pair and rider
  (`uq_route_requests_open`, partial on open, live rows).
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEXT_FIELDS = (
    "from_area_name",
    "to_area_name",
    "rider_name",
    "school_name",
    "contact_name",
    "notes",
    "decline_reason",
)
_INDEXES = (
    ("ix_route_requests_created_at_id", ["created_at", "id"]),
    ("ix_route_requests_route_id", ["route_id"]),
    ("ix_route_requests_matched_route_id", ["matched_route_id"]),
    ("ix_route_requests_from_area_id", ["from_area_id"]),
    ("ix_route_requests_to_area_id", ["to_area_id"]),
)


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


def _references(column: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"gize.{table}.id"],
        name=op.f(f"fk_route_requests_{column}_{table}"),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "route_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("from_area_id", sa.Uuid(), nullable=True),
        sa.Column("from_area_name", sa.String(length=100), nullable=True),
        sa.Column("to_area_id", sa.Uuid(), nullable=True),
        sa.Column("to_area_name", sa.String(length=100), nullable=True),
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("rider_type", sa.String(length=20), nullable=False),
        sa.Column("rider_name", sa.String(length=100), nullable=False),
        sa.Column("school_name", sa.String(length=100), nullable=True),
        sa.Column("preferred_morning_time", sa.Time(), nullable=True),
        sa.Column("preferred_return_time", sa.Time(), nullable=True),
        sa.Column("contact_name", sa.String(length=100), nullable=False),
        sa.Column("contact_phone", sa.String(length=13), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="staff", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column("matched_route_id", sa.Uuid(), nullable=True),
        sa.Column("decline_reason", sa.String(length=100), nullable=True),
        *_timestamps(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('new_areas', 'route')", name=op.f("ck_route_requests_kind")),
        sa.CheckConstraint(
            "(kind = 'route' AND route_id IS NOT NULL AND from_area_id IS NULL"
            " AND from_area_name IS NULL AND to_area_id IS NULL AND to_area_name IS NULL)"
            " OR (kind = 'new_areas' AND route_id IS NULL"
            " AND (from_area_id IS NULL) <> (from_area_name IS NULL)"
            " AND (to_area_id IS NULL) <> (to_area_name IS NULL))",
            name=op.f("ck_route_requests_kind_fields"),
        ),
        sa.CheckConstraint(
            "(from_area_id IS NULL OR to_area_id IS NULL OR from_area_id <> to_area_id)"
            " AND (from_area_name IS NULL OR to_area_name IS NULL"
            " OR lower(from_area_name) <> lower(to_area_name))",
            name=op.f("ck_route_requests_ends"),
        ),
        *(
            sa.CheckConstraint(
                f"{field} IS NULL OR ({field} = btrim({field}) AND {field} <> '')",
                name=op.f(f"ck_route_requests_{field}"),
            )
            for field in _TEXT_FIELDS
        ),
        sa.CheckConstraint(
            "rider_type IN ('adult', 'student')", name=op.f("ck_route_requests_rider_type")
        ),
        sa.CheckConstraint(
            "(rider_type = 'student') = (school_name IS NOT NULL)",
            name=op.f("ck_route_requests_school"),
        ),
        sa.CheckConstraint(
            "contact_phone ~ '^\\+251[79][0-9]{8}$'", name=op.f("ck_route_requests_contact_phone")
        ),
        sa.CheckConstraint(
            "preferred_morning_time IS NULL OR preferred_return_time IS NULL"
            " OR preferred_morning_time < preferred_return_time",
            name=op.f("ck_route_requests_preferred_times"),
        ),
        sa.CheckConstraint("source IN ('staff', 'app')", name=op.f("ck_route_requests_source")),
        sa.CheckConstraint(
            "status IN ('new', 'matched', 'fulfilled', 'declined')"
            " AND (status <> 'matched' OR matched_route_id IS NOT NULL)"
            " AND (status <> 'new' OR matched_route_id IS NULL)"
            " AND (status = 'declined') = (decline_reason IS NOT NULL)",
            name=op.f("ck_route_requests_status"),
        ),
        _references("from_area_id", "areas"),
        _references("to_area_id", "areas"),
        _references("route_id", "routes"),
        _references("matched_route_id", "routes"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_requests")),
        schema="gize",
    )
    op.create_index(
        "uq_route_requests_open",
        "route_requests",
        [
            "contact_phone",
            "kind",
            sa.literal_column("coalesce(route_id::text, '')"),
            sa.literal_column("coalesce(from_area_id::text, lower(from_area_name), '')"),
            sa.literal_column("coalesce(to_area_id::text, lower(to_area_name), '')"),
            sa.literal_column("lower(rider_name)"),
        ],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("deleted_at IS NULL AND status IN ('new', 'matched')"),
    )
    for name, columns in _INDEXES:
        op.create_index(name, "route_requests", columns, schema="gize")
    enable_row_level_security(op, "route_requests")


def downgrade() -> None:
    disable_row_level_security(op, "route_requests")
    for name, _ in reversed(_INDEXES):
        op.drop_index(name, table_name="route_requests", schema="gize")
    op.drop_index("uq_route_requests_open", table_name="route_requests", schema="gize")
    op.drop_table("route_requests", schema="gize")
