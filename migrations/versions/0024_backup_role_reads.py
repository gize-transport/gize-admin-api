"""The nightly backup's role reads every table (GZ-086).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-24

Expand only. `gize_backup` comes from the bootstrap (run it first: RUNBOOK, "Backups and
restoring"); default privileges give it SELECT on tables created from now on, and the RLS
helper its read-only policy. Here the tables that already exist get both: SELECT on every
table and sequence in `gize`, and the policy `gize_backup_read` (FOR SELECT, USING true)
on each table that lacks it. On a database built from scratch, the helper has already
made it with each table, so only the version table needs one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gize_backup') THEN
                RAISE EXCEPTION 'role gize_backup does not exist: run the bootstrap first'
                    USING HINT = 'RUNBOOK.md, "Backups and restoring"';
            END IF;
        END
        $$
        """
    )
    op.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "gize" TO "gize_backup"')
    op.execute('GRANT SELECT ON ALL SEQUENCES IN SCHEMA "gize" TO "gize_backup"')
    # Table names come from the catalog and are quoted by format(%I).
    op.execute(
        """
        DO $$
        DECLARE
            name text;
        BEGIN
            FOR name IN
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'gize' AND c.relkind IN ('r', 'p')
                AND NOT EXISTS (
                    SELECT 1 FROM pg_policy p
                    WHERE p.polrelid = c.oid AND p.polname = 'gize_backup_read'
                )
            LOOP
                EXECUTE format(
                    'CREATE POLICY "gize_backup_read" ON "gize".%I'
                    ' FOR SELECT TO "gize_backup" USING (true)',
                    name
                );
            END LOOP;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            name text;
        BEGIN
            FOR name IN
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_policy p ON p.polrelid = c.oid
                WHERE n.nspname = 'gize' AND p.polname = 'gize_backup_read'
            LOOP
                EXECUTE format('DROP POLICY "gize_backup_read" ON "gize".%I', name);
            END LOOP;
        END
        $$
        """
    )
    op.execute('REVOKE ALL ON ALL TABLES IN SCHEMA "gize" FROM "gize_backup"')
    op.execute('REVOKE ALL ON ALL SEQUENCES IN SCHEMA "gize" FROM "gize_backup"')
