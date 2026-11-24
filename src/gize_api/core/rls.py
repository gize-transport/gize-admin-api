"""Row-level security for every table in schema `gize` (defence in depth).

RLS is enabled with one permissive policy for `gize_app`, a read-only one for
`gize_backup` (GZ-086: the nightly backup reads every row through it), and none for
Supabase's `anon`, `authenticated` or `service_role`: even a mistaken grant exposes no rows
to them. Per-user authorization still lives in the services. The owner (`gize_migrator`)
bypasses RLS, which migrations rely on; RLS is not forced.

Migrations before 0024 call the same helper: on a new database they create the backup
policy with their tables, and 0024 adds it only where it is missing.

Every migration that creates a table calls `enable_row_level_security(op, "table")`.
"""

from typing import Protocol

from gize_api.core.models import SCHEMA

APP_ROLE = "gize_app"
BACKUP_ROLE = "gize_backup"


class _Executes(Protocol):
    """What the helpers need from Alembic's `op` (the module or an Operations object)."""

    def execute(self, sqltext: str) -> object: ...


POLICY = "gize_app_all"
BACKUP_POLICY = "gize_backup_read"


def row_level_security_statements(table: str) -> list[str]:
    """DDL for one table. `table` is a code constant (DDL takes no bind parameters)."""
    qualified = f'"{SCHEMA}"."{_identifier(table)}"'
    return [
        f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY",
        f'CREATE POLICY "{POLICY}" ON {qualified} FOR ALL TO "{APP_ROLE}"'
        " USING (true) WITH CHECK (true)",
        backup_policy_statement(table),
    ]


def backup_policy_statement(table: str) -> str:
    """gize_backup reads every row of `table`, and nothing else (GZ-086)."""
    qualified = f'"{SCHEMA}"."{_identifier(table)}"'
    return (
        f'CREATE POLICY "{BACKUP_POLICY}" ON {qualified} FOR SELECT TO "{BACKUP_ROLE}" USING (true)'
    )


def enable_row_level_security(op: _Executes, table: str) -> None:
    for statement in row_level_security_statements(table):
        op.execute(statement)


def disable_row_level_security(op: _Executes, table: str) -> None:
    qualified = f'"{SCHEMA}"."{_identifier(table)}"'
    op.execute(f'DROP POLICY IF EXISTS "{BACKUP_POLICY}" ON {qualified}')
    op.execute(f'DROP POLICY IF EXISTS "{POLICY}" ON {qualified}')
    op.execute(f"ALTER TABLE {qualified} DISABLE ROW LEVEL SECURITY")


def _identifier(name: str) -> str:
    if not name.replace("_", "").isalnum() or not name.isascii() or name != name.lower():
        raise ValueError(f"not a plain lower-case identifier: {name!r}")
    return name
