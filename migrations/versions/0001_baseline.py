"""Baseline: protect the migration table.

Revision ID: 0001
Revises:
Create Date: 2026-10-28

The schema, roles and grants come from the bootstrap (gize_api.core.bootstrap). Here the
version table, created by Alembic as gize_migrator, gets row-level security and loses the
DML that default privileges granted gize_app: the app never reads or changes it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('ALTER TABLE "gize"."alembic_version" ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON "gize"."alembic_version" FROM "gize_app"')


def downgrade() -> None:
    # The revoked grant is deliberately not restored: gize_app never needs the version table,
    # and widening a privilege on the way down would make a rollback less safe.
    op.execute('ALTER TABLE "gize"."alembic_version" DISABLE ROW LEVEL SECURITY')
