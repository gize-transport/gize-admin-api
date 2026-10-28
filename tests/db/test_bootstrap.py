"""GZ-006: the one-off bootstrap (roles, schema, grants) is safe to run again."""

import asyncpg
import pytest

from gize_api.core.bootstrap import BootstrapError, bootstrap, role_from_url
from tests.support import DatabaseUrls


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
