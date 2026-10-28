"""GZ-006: the one-off bootstrap (roles, schema, grants) is safe to run again."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import asyncpg
import pytest

from gize_api.core.bootstrap import BootstrapError, bootstrap, role_from_url
from tests.support import DatabaseUrls, database_urls, run_migrations


async def test_bootstrap_is_idempotent(database: DatabaseUrls) -> None:
    for _ in range(2):
        await bootstrap(database.admin, database.app, database.migrator, create_supabase_roles=True)
    conn = await asyncpg.connect(database.app, timeout=3)
    try:
        assert await conn.fetchval("SELECT 1") == 1  # the password still matches the URL
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("url", "role"),
    [
        ("postgresql://gize_app:pw@db:5432/gize", "gize_app"),
        ("postgresql://gize_app.abcdefghijklmnop:pw@pooler:5432/postgres", "gize_app"),
    ],
)
def test_role_from_url_understands_the_supabase_pooler(url: str, role: str) -> None:
    assert role_from_url(url) == role


@pytest.mark.parametrize(
    ("app_url", "migrator_url"),
    [
        ("postgresql://postgres:pw@db/gize", "postgresql://gize_migrator:pw@db/gize"),
        ("postgresql://gize_app:pw@db/gize", "postgresql://gize_app:pw@db/gize"),
        ("postgresql://gize_app@db/gize", "postgresql://gize_migrator:pw@db/gize"),
    ],
)
async def test_bootstrap_rejects_unexpected_roles_or_missing_passwords(
    database: DatabaseUrls, app_url: str, migrator_url: str
) -> None:
    with pytest.raises(BootstrapError) as exc_info:
        await bootstrap(database.admin, app_url, migrator_url, create_supabase_roles=False)
    assert "pw" not in str(exc_info.value)


async def test_bootstrap_refuses_a_role_with_dangerous_attributes(database: DatabaseUrls) -> None:
    admin = await asyncpg.connect(database.admin, timeout=3)
    try:
        await admin.execute('ALTER ROLE "gize_migrator" CREATEDB')
        with pytest.raises(BootstrapError, match="rolcreatedb"):
            await bootstrap(
                database.admin, database.app, database.migrator, create_supabase_roles=False
            )
    finally:
        await admin.execute('ALTER ROLE "gize_migrator" NOCREATEDB')
        await admin.close()


def test_setting_values_are_checked() -> None:
    from gize_api.core.bootstrap import _quote

    assert _quote("5min") == "'5min'"
    with pytest.raises(BootstrapError):
        _quote("5s'; DROP ROLE x; --")


def test_scram_verifier_shape() -> None:
    from gize_api.core.bootstrap import scram_verifier

    verifier = scram_verifier("correct horse")
    assert verifier.startswith("SCRAM-SHA-256$4096:")
    assert "correct horse" not in verifier
    assert verifier != scram_verifier("correct horse")  # fresh salt every time


def test_cli(
    database: DatabaseUrls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gize_api.core import bootstrap as module

    monkeypatch.setattr("sys.argv", ["bootstrap"])
    for name in ("BOOTSTRAP_ADMIN_DATABASE_URL", "DATABASE_URL", "MIGRATOR_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert module.main() == 2
    assert "BOOTSTRAP_ADMIN_DATABASE_URL" in capsys.readouterr().err

    monkeypatch.setenv("BOOTSTRAP_ADMIN_DATABASE_URL", database.admin)
    monkeypatch.setenv("DATABASE_URL", database.app)
    monkeypatch.setenv("MIGRATOR_DATABASE_URL", database.app)  # wrong role
    assert module.main() == 1
    err = capsys.readouterr().err
    assert "gize_migrator" in err
    assert "gize-app-test" not in err  # no password in the message

    monkeypatch.setenv("MIGRATOR_DATABASE_URL", database.migrator)
    monkeypatch.setattr("sys.argv", ["bootstrap", "--create-supabase-roles"])
    assert module.main() == 0


async def test_missing_roles_are_created(database: DatabaseUrls) -> None:
    from gize_api.core.bootstrap import _create_role

    conn = await asyncpg.connect(database.admin, timeout=3)
    try:
        await conn.execute('DROP ROLE IF EXISTS "gize_bootstrap_probe"')
        await _create_role(conn, "gize_bootstrap_probe", login=False)
        row = await conn.fetchrow(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = 'gize_bootstrap_probe'"
        )
        assert row is not None
        assert (row["rolcanlogin"], row["rolsuper"]) == (False, False)
    finally:
        await conn.execute('DROP ROLE IF EXISTS "gize_bootstrap_probe"')
        await conn.close()


async def _admin(database: DatabaseUrls) -> asyncpg.Connection:
    return await asyncpg.connect(database.admin, timeout=3)


async def _bootstrap(database: DatabaseUrls, admin_url: str | None = None) -> None:
    await bootstrap(
        admin_url or database.admin, database.app, database.migrator, create_supabase_roles=False
    )


@pytest.mark.parametrize(
    ("setup", "teardown", "message"),
    [
        (
            "GRANT gize_migrator TO gize_app",
            "REVOKE gize_migrator FROM gize_app",
            "gize_app must not be a member of gize_migrator",
        ),
        (
            "GRANT pg_read_all_data TO gize_migrator",
            "REVOKE pg_read_all_data FROM gize_migrator",
            "gize_migrator must not be a member of pg_read_all_data",
        ),
        (
            "GRANT CREATE ON SCHEMA gize TO gize_app",
            "REVOKE CREATE ON SCHEMA gize FROM gize_app",
            "gize_app must not have CREATE on schema gize",
        ),
        (
            'GRANT CREATE ON DATABASE "{db}" TO gize_app',
            'REVOKE CREATE ON DATABASE "{db}" FROM gize_app',
            "gize_app must not have CREATE on the database",
        ),
        (
            "GRANT TRUNCATE ON gize.alembic_version TO gize_app",
            "REVOKE TRUNCATE ON gize.alembic_version FROM gize_app",
            "gize_app must not have TRUNCATE on gize.alembic_version",
        ),
        (
            "CREATE TABLE gize.owned_by_app (id int);"
            " ALTER TABLE gize.owned_by_app OWNER TO gize_app",
            "DROP TABLE gize.owned_by_app",
            "gize_app must not own gize.owned_by_app",
        ),
    ],
)
async def test_bootstrap_refuses_privilege_drift(
    database: DatabaseUrls, setup: str, teardown: str, message: str
) -> None:
    """Re-running never blesses widened privileges: it stops and names the problem."""
    admin = await _admin(database)
    try:
        await admin.execute(setup.format(db=database.name))
        with pytest.raises(BootstrapError, match=message):
            await _bootstrap(database)
    finally:
        await admin.execute(teardown.format(db=database.name))
        await admin.close()


async def test_rerun_keeps_narrower_grants_set_by_migrations(database: DatabaseUrls) -> None:
    migrator = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await migrator.execute("CREATE TABLE gize.append_only (id int)")
        await migrator.execute("REVOKE UPDATE, DELETE ON gize.append_only FROM gize_app")
        await _bootstrap(database)
        assert not await migrator.fetchval(
            "SELECT has_table_privilege('gize_app', 'gize.append_only', 'DELETE')"
        )
    finally:
        await migrator.execute("DROP TABLE IF EXISTS gize.append_only")
        await migrator.close()


async def test_rerun_removes_a_password_expiry(database: DatabaseUrls) -> None:
    admin = await _admin(database)
    try:
        await admin.execute("ALTER ROLE gize_app VALID UNTIL '2030-01-01'")
        await _bootstrap(database)
        expiry = await admin.fetchval(
            "SELECT rolvaliduntil::text FROM pg_roles WHERE rolname = 'gize_app'"
        )
    finally:
        await admin.close()
    assert expiry in {None, "infinity"}


async def test_admin_keeps_no_membership_in_the_migrator(database: DatabaseUrls) -> None:
    """Otherwise objects the admin creates in `gize` would skip default privileges and RLS."""
    await _bootstrap(database)
    admin = await _admin(database)
    try:
        memberships = await admin.fetch(
            "SELECT 1 FROM pg_auth_members WHERE roleid = 'gize_migrator'::regrole"
            " AND member = current_user::regrole"
        )
    finally:
        await admin.close()
    assert memberships == []


async def test_bootstrap_gives_up_on_a_held_lock(
    database: DatabaseUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gize_api.core.bootstrap.ADMIN_LOCK_TIMEOUT", "200ms")
    holder = await _admin(database)
    try:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute("ALTER ROLE gize_app VALID UNTIL 'infinity'")
        with pytest.raises(BootstrapError, match="waited too long for a lock"):
            await _bootstrap(database)
        await transaction.rollback()
    finally:
        await holder.close()


@pytest.fixture
async def supabase_like_admin(database: DatabaseUrls) -> AsyncIterator[DatabaseUrls]:
    """A database owned by a CREATEROLE admin that is not a superuser, as on Supabase.

    The roles already exist (server-wide); the admin holds ADMIN OPTION on them without SET
    or INHERIT, which is what Postgres 16+ gives a CREATEROLE role for roles it creates.
    """
    name = f"{database.name}_supa"
    admin = await _admin(database)
    try:
        await admin.execute("DROP ROLE IF EXISTS gize_test_admin")
        await admin.execute(
            "CREATE ROLE gize_test_admin LOGIN CREATEROLE NOSUPERUSER PASSWORD 'supa-test'"
        )
        await admin.execute(
            "GRANT gize_app, gize_migrator TO gize_test_admin"
            " WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"
        )
        await admin.execute(f'CREATE DATABASE "{name}" OWNER gize_test_admin')
        urls = database_urls(database.admin, name)
        server = urlsplit(database.admin).netloc.rsplit("@", 1)[1]
        yield dataclasses.replace(
            urls, admin=f"postgresql://gize_test_admin:supa-test@{server}/{name}"
        )
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute("DROP ROLE IF EXISTS gize_test_admin")
        await admin.close()


async def test_bootstrap_as_a_non_superuser_admin_twice(supabase_like_admin: DatabaseUrls) -> None:
    urls = supabase_like_admin
    for _ in range(2):
        await bootstrap(urls.admin, urls.app, urls.migrator, create_supabase_roles=False)
    await asyncio.to_thread(run_migrations, urls.migrator, "head")  # Alembic runs its own loop
    admin = await asyncpg.connect(urls.admin, timeout=3)
    try:
        options = await admin.fetch(
            "SELECT set_option, inherit_option FROM pg_auth_members"
            " WHERE roleid = 'gize_migrator'::regrole AND member = current_user::regrole"
        )
        owner = await admin.fetchval(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'gize'"
        )
    finally:
        await admin.close()
    assert owner == "gize_migrator"
    assert all(not row["set_option"] and not row["inherit_option"] for row in options)
    app = await asyncpg.connect(urls.app, timeout=3)
    try:
        assert await app.fetchval("SELECT has_schema_privilege('gize', 'USAGE')")
    finally:
        await app.close()


async def test_rerun_replaces_changed_role_settings(database: DatabaseUrls) -> None:
    """A role may change its own settings (e.g. no statement timeout); a re-run undoes it."""
    app = await asyncpg.connect(database.app, timeout=3)
    try:
        await app.execute("ALTER ROLE gize_app SET statement_timeout = 0")
        await app.execute("ALTER ROLE gize_app SET work_mem = '64MB'")
    finally:
        await app.close()
    await _bootstrap(database)
    admin = await _admin(database)
    try:
        settings = await admin.fetchval(
            "SELECT setconfig FROM pg_db_role_setting"
            " WHERE setrole = 'gize_app'::regrole AND setdatabase = 0"
        )
    finally:
        await admin.close()
    assert sorted(settings) == [
        "idle_in_transaction_session_timeout=10s",
        "lock_timeout=3s",
        "search_path=gize",
        "statement_timeout=5s",
    ]
