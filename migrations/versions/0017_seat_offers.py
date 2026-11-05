"""Seat offers (GZ-040).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05

Expand only: a new table, with row-level security like every table; `sms_messages.purpose`
also takes `seat_offer`.
- `seat_offers`: a free seat offered to an open route request for 48 hours; one open offer
  per request (`uq_seat_offers_request_id_open`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gize_api.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _purposes(*purposes: str) -> None:
    op.drop_constraint(
        op.f("ck_sms_messages_purpose"), "sms_messages", type_="check", schema="gize"
    )
    listed = ", ".join(f"'{purpose}'" for purpose in purposes)  # fixed names, not input
    op.create_check_constraint(
        op.f("ck_sms_messages_purpose"), "sms_messages", f"purpose IN ({listed})", schema="gize"
    )


def upgrade() -> None:
    op.create_table(
        "seat_offers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("rider_id", sa.Uuid(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("skip_reason", sa.String(length=100), nullable=True),
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
            "status IN ('open', 'accepted', 'expired', 'withdrawn')",
            name=op.f("ck_seat_offers_status"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (rider_id IS NOT NULL)", name=op.f("ck_seat_offers_accepted")
        ),
        sa.ForeignKeyConstraint(
            ["route_id"], ["gize.routes.id"], name=op.f("fk_seat_offers_route_id_routes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["gize.route_requests.id"],
            name=op.f("fk_seat_offers_request_id_route_requests"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rider_id"], ["gize.riders.id"], name=op.f("fk_seat_offers_rider_id_riders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seat_offers")),
        schema="gize",
    )  # fmt: skip
    op.create_index(
        "uq_seat_offers_request_id_open",
        "seat_offers",
        ["request_id"],
        unique=True,
        schema="gize",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_seat_offers_route_id_created_at",
        "seat_offers",
        ["route_id", "created_at"],
        schema="gize",
    )
    enable_row_level_security(op, "seat_offers")
    _purposes("phone_verification", "route_launch", "seat_offer")


def downgrade() -> None:
    # Offer SMS are records of messages that went out: refuse rather than delete them.
    offer_rows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM gize.sms_messages WHERE purpose = 'seat_offer'")
    )
    if offer_rows:
        raise RuntimeError(
            f"{offer_rows} seat offer SMS records exist (GZ-040): this downgrade would have to"
            " delete them. Roll forward instead."
        )
    _purposes("phone_verification", "route_launch")
    disable_row_level_security(op, "seat_offers")
    op.drop_index("ix_seat_offers_route_id_created_at", table_name="seat_offers", schema="gize")
    op.drop_index("uq_seat_offers_request_id_open", table_name="seat_offers", schema="gize")
    op.drop_table("seat_offers", schema="gize")
