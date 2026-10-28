"""GZ-006: least privilege. The app role can read and write data, and nothing else."""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from gize_api.core.rls import row_level_security_statements
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


@pytest.fixture
async def data_table(database: DatabaseUrls) -> AsyncIterator[str]:
    """A table created by the migrator as a migration would, dropped afterwards."""
    conn = await _connect(database.migrator)
    try:
        await conn.execute("CREATE TABLE gize.test_data (id int PRIMARY KEY)")
        for statement in row_level_security_statements("test_data"):
            await conn.execute(statement)
        await conn.execute(
            "CREATE FUNCTION gize.test_fn() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
        )
        yield "gize.test_data"
    finally:
        await conn.execute("DROP FUNCTION IF EXISTS gize.test_fn()")
        await conn.execute("DROP TABLE IF EXISTS gize.test_data")
        await conn.close()


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE gize.test_data",
        "TRUNCATE gize.test_data",
        "ALTER TABLE gize.test_data DISABLE ROW LEVEL SECURITY",
        "CREATE INDEX intruder ON gize.test_data (id)",
        "CREATE TRIGGER intruder BEFORE INSERT ON gize.test_data"
        " FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
    ],
)
@pytest.mark.usefixtures("data_table")
async def test_app_role_cannot_change_a_data_table(database: DatabaseUrls, statement: str) -> None:
    conn = await _connect(database.app)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.usefixtures("data_table")
async def test_new_tables_grant_the_app_role_data_access_only(database: DatabaseUrls) -> None:
    conn = await _connect(database.admin)
    try:
        granted = {
            privilege
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
                "MAINTAIN",
            )
            if await conn.fetchval(
                "SELECT has_table_privilege('gize_app', 'gize.test_data', $1)", privilege
            )
        }
    finally:
        await conn.close()
    assert granted == {"SELECT", "INSERT", "UPDATE", "DELETE"}


@pytest.mark.parametrize("role", ["anon", "authenticated", "service_role", "public"])
@pytest.mark.usefixtures("data_table")
async def test_new_functions_are_not_executable_by_other_roles(
    database: DatabaseUrls, role: str
) -> None:
    conn = await _connect(database.admin)
    try:
        acl: list[str] | None = await conn.fetchval(
            "SELECT proacl::text[] FROM pg_proc WHERE oid = 'gize.test_fn()'::regprocedure"
        )
        if role == "public":
            assert acl is not None  # NULL would mean the built-in default: PUBLIC may execute
            assert not [item for item in acl if item.startswith("=")]  # PUBLIC has no grantee
            return
        assert not await conn.fetchval(
            "SELECT has_function_privilege($1, 'gize.test_fn()', 'EXECUTE')", role
        )
        assert await conn.fetchval(
            "SELECT has_function_privilege('gize_app', 'gize.test_fn()', 'EXECUTE')"
        )
    finally:
        await conn.close()


@pytest.mark.usefixtures("data_table")
async def test_every_policy_is_for_the_app_role_only(database: DatabaseUrls) -> None:
    conn = await _connect(database.admin)
    try:
        rows = await conn.fetch(
            "SELECT tablename, policyname, roles::text[] AS roles FROM pg_policies"
            " WHERE schemaname = 'gize'"
        )
    finally:
        await conn.close()
    assert rows  # the probe table has its policy
    assert {(row["tablename"], tuple(row["roles"])) for row in rows} == {
        (row["tablename"], ("gize_app",)) for row in rows
    }


async def test_every_view_runs_as_the_caller(database: DatabaseUrls) -> None:
    """A view runs as its owner unless security_invoker is set, which would bypass RLS."""
    conn = await _connect(database.admin)
    try:
        unsafe = await conn.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'gize' AND c.relkind = 'v'"
            " AND NOT coalesce('security_invoker=true' = ANY(c.reloptions), false)"
        )
    finally:
        await conn.close()
    assert [row["relname"] for row in unsafe] == []


async def test_app_role_search_path_is_the_gize_schema(database: DatabaseUrls) -> None:
    conn = await _connect(database.app)
    try:
        assert await conn.fetchval("SHOW search_path") == "gize"
    finally:
        await conn.close()


async def test_service_role_stand_in_bypasses_rls_like_supabase(database: DatabaseUrls) -> None:
    """Supabase's service_role has BYPASSRLS, so schema access is its only barrier."""
    conn = await _connect(database.admin)
    try:
        assert await conn.fetchval(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'service_role'"
        )
    finally:
        await conn.close()
