"""GZ-006: least privilege. The app role can read and write data, and nothing else."""

import asyncpg
import pytest

from tests.support import DatabaseUrls

pytestmark = pytest.mark.asyncio


async def _connect(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(url, timeout=3)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE gize.intruder (id int)",
        "CREATE TABLE public.intruder (id int)",
        "CREATE SCHEMA intruder",
        "DROP TABLE gize.alembic_version",
        "ALTER TABLE gize.alembic_version ADD COLUMN x int",
        "TRUNCATE gize.alembic_version",
        "CREATE ROLE intruder",
        "CREATE DATABASE intruder",
    ],
)
async def test_app_role_cannot_change_structure_or_roles(
    database: DatabaseUrls, statement: str
) -> None:
    conn = await _connect(database.app)
    try:
        with pytest.raises(asyncpg.exceptions.PostgresError):
            await conn.execute(statement)
    finally:
        await conn.close()


async def test_app_role_cannot_read_the_migration_table(database: DatabaseUrls) -> None:
    conn = await _connect(database.app)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM gize.alembic_version")
    finally:
        await conn.close()


async def test_role_attributes_are_minimal(database: DatabaseUrls) -> None:
    conn = await _connect(database.admin)
    try:
        rows = await conn.fetch(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication,"
            " rolinherit, rolcanlogin FROM pg_roles WHERE rolname = ANY($1::text[])",
            ["gize_app", "gize_migrator"],
        )
    finally:
        await conn.close()
    roles = {row["rolname"]: dict(row) for row in rows}
    for name in ("gize_app", "gize_migrator"):
        role = roles[name]
        assert role["rolcanlogin"]
        for attribute in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolbypassrls",
            "rolreplication",
        ):
            assert role[attribute] is False, (name, attribute)
    assert roles["gize_app"]["rolinherit"] is False


@pytest.mark.parametrize(
    ("role", "setting", "expected"),
    [
        ("gize_app", "statement_timeout", "5s"),
        ("gize_app", "lock_timeout", "3s"),
        ("gize_app", "idle_in_transaction_session_timeout", "10s"),
        ("gize_migrator", "lock_timeout", "5s"),
    ],
)
async def test_role_level_timeouts(
    database: DatabaseUrls, role: str, setting: str, expected: str
) -> None:
    url = database.app if role == "gize_app" else database.migrator
    conn = await _connect(url)
    try:
        assert await conn.fetchval(f"SHOW {setting}") == expected
    finally:
        await conn.close()


@pytest.mark.parametrize("supabase_role", ["anon", "authenticated", "service_role"])
async def test_supabase_api_roles_cannot_use_the_schema(
    database: DatabaseUrls, supabase_role: str
) -> None:
    conn = await _connect(database.admin)
    try:
        has_usage = await conn.fetchval(
            "SELECT has_schema_privilege($1, 'gize', 'USAGE')", supabase_role
        )
        await conn.execute(f'SET ROLE "{supabase_role}"')
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM gize.alembic_version")
    finally:
        await conn.close()
    assert has_usage is False


async def test_every_table_in_the_schema_has_row_level_security(database: DatabaseUrls) -> None:
    conn = await _connect(database.admin)
    try:
        unprotected = await conn.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'gize' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity"
        )
    finally:
        await conn.close()
    assert [row["relname"] for row in unprotected] == []


async def test_schema_is_owned_by_the_migrator(database: DatabaseUrls) -> None:
    conn = await _connect(database.admin)
    try:
        owner = await conn.fetchval(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'gize'"
        )
    finally:
        await conn.close()
    assert owner == "gize_migrator"
