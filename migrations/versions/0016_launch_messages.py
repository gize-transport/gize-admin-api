"""Launch messages (GZ-041).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-05

Expand only:
- `route_assignments.launched_on`: the launch day, on each reservation a launch started
  (GZ-024's `start_reserved`): who gets the launch SMS, and whom staff may resend it to.
- `sms_messages.purpose` also takes `route_launch`; `ix_sms_messages_route_launch`
  (partial) lets the status view read launch rows without the phone-code ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_assignments", sa.Column("launched_on", sa.Date(), nullable=True), schema="gize"
    )
    op.drop_constraint(
        op.f("ck_sms_messages_purpose"), "sms_messages", type_="check", schema="gize"
    )
    op.create_check_constraint(
        op.f("ck_sms_messages_purpose"),
        "sms_messages",
        "purpose IN ('phone_verification', 'route_launch')",
        schema="gize",
    )
    op.create_index(
        "ix_sms_messages_route_launch",
        "sms_messages",
        ["dedup_key"],
        schema="gize",
        postgresql_where=sa.text("purpose = 'route_launch'"),
    )


def downgrade() -> None:
    # Launch messages are records of SMS that went out: refuse rather than delete them.
    launch_rows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM gize.sms_messages WHERE purpose = 'route_launch'")
    )
    if launch_rows:
        raise RuntimeError(
            f"{launch_rows} launch SMS records exist (GZ-041): this downgrade would have to"
            " delete them. Roll forward instead."
        )
    op.drop_index("ix_sms_messages_route_launch", table_name="sms_messages", schema="gize")
    op.drop_constraint(
        op.f("ck_sms_messages_purpose"), "sms_messages", type_="check", schema="gize"
    )
    op.create_check_constraint(
        op.f("ck_sms_messages_purpose"),
        "sms_messages",
        "purpose IN ('phone_verification')",
        schema="gize",
    )
    op.drop_column("route_assignments", "launched_on", schema="gize")
