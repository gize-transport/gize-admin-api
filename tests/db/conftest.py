"""Fixtures shared by database tests."""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from gize_api.core.rls import row_level_security_statements
from tests.support import DatabaseUrls


@pytest.fixture
async def widgets_table(database: DatabaseUrls) -> AsyncIterator[None]:
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await conn.execute(
            "CREATE TABLE gize.test_widgets ("
            " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            " label varchar(50) NOT NULL,"
            " created_at timestamptz NOT NULL DEFAULT now(),"
            " updated_at timestamptz NOT NULL DEFAULT now(),"
            " deleted_at timestamptz)"
        )
        for statement in row_level_security_statements("test_widgets"):
            await conn.execute(statement)
        yield
    finally:
        await conn.execute("DROP TABLE IF EXISTS gize.test_widgets")
        await conn.close()
