"""GZ-006: the migration helpers turn row-level security on and off on a real table."""

import asyncpg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from gize_api.core.db import async_url
from gize_api.core.rls import disable_row_level_security, enable_row_level_security
from tests.support import DatabaseUrls


async def _policies(admin_url: str) -> set[tuple[str, str]]:
    conn = await asyncpg.connect(admin_url, timeout=3)
    try:
        rows = await conn.fetch(
            "SELECT policyname, cmd FROM pg_policies"
            " WHERE schemaname = 'gize' AND tablename = 'rls_probe'"
        )
    finally:
        await conn.close()
    return {(row["policyname"], row["cmd"]) for row in rows}


async def _rls_enabled(admin_url: str) -> bool:
    conn = await asyncpg.connect(admin_url, timeout=3)
    try:
        return bool(
            await conn.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid = 'gize.rls_probe'::regclass"
            )
        )
    finally:
        await conn.close()


async def test_enable_and_disable(database: DatabaseUrls) -> None:
    engine = create_async_engine(async_url(database.migrator), poolclass=NullPool)

    def create_and_enable(connection: Connection) -> None:
        op = Operations(MigrationContext.configure(connection))
        op.execute("CREATE TABLE gize.rls_probe (id int)")
        enable_row_level_security(op, "rls_probe")

    def disable(connection: Connection) -> None:
        disable_row_level_security(Operations(MigrationContext.configure(connection)), "rls_probe")

    try:
        async with engine.begin() as conn:
            await conn.run_sync(create_and_enable)
        assert await _rls_enabled(database.admin)
        # GZ-086: the backup role's read-only policy comes with the app's.
        assert await _policies(database.admin) == {
            ("gize_app_all", "ALL"),
            ("gize_backup_read", "SELECT"),
        }
        async with engine.begin() as conn:
            await conn.run_sync(disable)
        assert not await _rls_enabled(database.admin)
        assert await _policies(database.admin) == set()
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DROP TABLE IF EXISTS gize.rls_probe")
        await engine.dispose()


@pytest.mark.parametrize("name", ["Riders", "riders; DROP TABLE x", "rïders", "a-b"])
def test_only_plain_identifiers(name: str) -> None:
    from gize_api.core.rls import row_level_security_statements

    with pytest.raises(ValueError, match="identifier"):
        row_level_security_statements(name)
