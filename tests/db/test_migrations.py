"""GZ-006: migrations run as the migrator, and every migration can be undone."""

import asyncpg

from tests.support import DatabaseUrls, run_migrations


async def _revision(url: str) -> str | None:
    conn = await asyncpg.connect(url, timeout=3)
    try:
        revision: str | None = await conn.fetchval("SELECT version_num FROM gize.alembic_version")
        return revision
    finally:
        await conn.close()


def test_upgrade_downgrade_upgrade(scratch_database: DatabaseUrls) -> None:
    run_migrations(scratch_database.migrator, "head")
    run_migrations(scratch_database.migrator, "base", downgrade=True)
    run_migrations(scratch_database.migrator, "head")


async def test_head_is_recorded(database: DatabaseUrls) -> None:
    assert await _revision(database.migrator) is not None
