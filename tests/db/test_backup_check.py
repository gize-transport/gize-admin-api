"""GZ-086: the dump's first step, run as ops/backup/dump.sh runs it, against real tables.

Through row-level security a table without gize_backup's read policy (or with a restrictive
one) would dump short, not fail; this query names such tables, and the script then dumps
nothing.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from gize_api.core.rls import row_level_security_statements
from tests.support import DatabaseUrls

DUMP = (Path(__file__).resolve().parents[2] / "ops" / "backup" / "dump.sh").read_text()
UNREADABLE = DUMP.split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]


async def _unreadable(url: str) -> str:
    conn = await asyncpg.connect(url, timeout=3)
    try:
        result: str = await conn.fetchval(UNREADABLE)
    finally:
        await conn.close()
    return result


@pytest.fixture
async def probe(database: DatabaseUrls) -> AsyncIterator[asyncpg.Connection]:
    """A table made as a migration makes one (RLS and both policies), dropped after."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await conn.execute("CREATE TABLE gize.backup_probe (id int)")
        for statement in row_level_security_statements("backup_probe"):
            await conn.execute(statement)
        yield conn
    finally:
        await conn.execute("DROP TABLE IF EXISTS gize.backup_probe")
        await conn.close()


@pytest.mark.usefixtures("probe")
async def test_every_table_is_readable_as_migrations_make_them(database: DatabaseUrls) -> None:
    assert await _unreadable(database.backup) == ""


@pytest.mark.parametrize(
    "change",
    [
        'DROP POLICY "gize_backup_read" ON gize.backup_probe',
        'ALTER POLICY "gize_backup_read" ON gize.backup_probe USING (id > 0)',
        "CREATE POLICY hide ON gize.backup_probe AS RESTRICTIVE FOR SELECT TO PUBLIC USING (false)",
        "CREATE POLICY hide ON gize.backup_probe AS RESTRICTIVE FOR ALL TO gize_backup"
        " USING (false)",
        "REVOKE SELECT ON gize.backup_probe FROM gize_backup",
    ],
)
async def test_a_table_it_cannot_read_whole_is_named(
    database: DatabaseUrls, probe: asyncpg.Connection, change: str
) -> None:
    await probe.execute(change)
    assert await _unreadable(database.backup) == "backup_probe"


async def test_the_owner_reads_past_rls_for_a_copy_by_hand(
    database: DatabaseUrls, probe: asyncpg.Connection
) -> None:
    await probe.execute('DROP POLICY "gize_backup_read" ON gize.backup_probe')
    assert await _unreadable(database.migrator) == ""
    await probe.execute("ALTER TABLE gize.backup_probe FORCE ROW LEVEL SECURITY")
    assert "backup_probe" in await _unreadable(database.migrator)
