"""One-off database bootstrap: roles, schema, grants. Run by a human, safe to run again.

    uv run --env-file .env python -m gize_api.core.bootstrap [--create-supabase-roles]

Reads BOOTSTRAP_ADMIN_DATABASE_URL (the `postgres` role), DATABASE_URL (gize_app) and
MIGRATOR_DATABASE_URL (gize_migrator); role passwords are taken from the last two, so they
can never disagree. `--create-supabase-roles` creates stand-ins for Supabase's `anon`,
`authenticated` and `service_role` (local and test databases only).

Result:
- `gize_migrator` owns schema `gize` and runs migrations; `gize_app` gets SELECT, INSERT,
  UPDATE, DELETE on its tables and nothing else (no DDL, no TRUNCATE, no migration table).
- Neither role is superuser, creates roles or databases, replicates or bypasses RLS.
- Supabase's API roles and PUBLIC have no access to the schema.
- Role-level timeouts: gize_app statement 5 s, lock 3 s, idle in transaction 10 s.

Passwords are sent as SCRAM-SHA-256 verifiers computed here, never in plain text, so they
cannot appear in server statement logs.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sys
from urllib.parse import unquote, urlsplit

import asyncpg

APP_ROLE = "gize_app"
MIGRATOR_ROLE = "gize_migrator"
SCHEMA = "gize"
SUPABASE_API_ROLES = ("anon", "authenticated", "service_role")
SCRAM_ITERATIONS = 4096

_ROLE_SETTINGS = {
    APP_ROLE: {
        "statement_timeout": "5s",
        "lock_timeout": "3s",
        "idle_in_transaction_session_timeout": "10s",
    },
    MIGRATOR_ROLE: {"lock_timeout": "5s", "statement_timeout": "5min"},
}
_FORBIDDEN_ATTRIBUTES = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolbypassrls",
    "rolreplication",
)


class BootstrapError(Exception):
    """The bootstrap cannot proceed. The message never contains a URL or password."""


def role_from_url(url: str) -> str:
    """The role in a connection URL; Supabase's pooler spells it `role.project_ref`."""
    username = urlsplit(url).username or ""
    return unquote(username).split(".", 1)[0]


def scram_verifier(password: str) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for `password` (RFC 5802/7677)."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


async def bootstrap(
    admin_url: str, app_url: str, migrator_url: str, *, create_supabase_roles: bool
) -> None:
    """Create or update roles, schema and grants in the database `admin_url` points at."""
    passwords = {
        APP_ROLE: _password(app_url, APP_ROLE, "DATABASE_URL"),
        MIGRATOR_ROLE: _password(migrator_url, MIGRATOR_ROLE, "MIGRATOR_DATABASE_URL"),
    }
    conn = await asyncpg.connect(admin_url, timeout=10)
    try:
        async with conn.transaction():
            if create_supabase_roles:
                for role in SUPABASE_API_ROLES:
                    await _create_role(conn, role, login=False)
            for role, password in passwords.items():
                await _create_role(conn, role, login=True)
                await _set_password(conn, role, password)
                await _check_attributes(conn, role)
                for name, value in _ROLE_SETTINGS[role].items():
                    await conn.execute(f'ALTER ROLE "{role}" SET {name} = {_quote(value)}')
            await conn.execute(f'ALTER ROLE "{APP_ROLE}" NOINHERIT')
            await _grant_schema(conn)
    finally:
        await conn.close()


def _password(url: str, expected_role: str, variable: str) -> str:
    if role_from_url(url) != expected_role:
        raise BootstrapError(f"{variable} must connect as {expected_role}")
    password = unquote(urlsplit(url).password or "")
    if not password:
        raise BootstrapError(f"{variable} must include the {expected_role} password")
    return password


async def _create_role(conn: asyncpg.Connection, role: str, *, login: bool) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    if not exists:
        # Attributes left at their defaults (NOSUPERUSER, NOCREATEDB, NOBYPASSRLS, ...):
        # a non-superuser admin, as on Supabase, may not name them.
        await conn.execute(f'CREATE ROLE "{role}" {"LOGIN" if login else "NOLOGIN"}')


async def _set_password(conn: asyncpg.Connection, role: str, password: str) -> None:
    statement = await conn.fetchval(
        "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', $1::text, $2::text)",
        role,
        scram_verifier(password),
    )
    await conn.execute(statement)


async def _check_attributes(conn: asyncpg.Connection, role: str) -> None:
    row = await conn.fetchrow("SELECT * FROM pg_roles WHERE rolname = $1", role)
    granted = [name for name in _FORBIDDEN_ATTRIBUTES if row is not None and row[name]]
    if granted:
        raise BootstrapError(f"{role} must not have {', '.join(granted)}; fix it by hand first")


async def _grant_schema(conn: asyncpg.Connection) -> None:
    app, migrator, schema = f'"{APP_ROLE}"', f'"{MIGRATOR_ROLE}"', f'"{SCHEMA}"'
    database = await conn.fetchval("SELECT format('%I', current_database())")
    await conn.execute(f"GRANT CONNECT ON DATABASE {database} TO {app}, {migrator}")
    # Lets the admin create the schema for the migrator and set its default privileges.
    await conn.execute(f"GRANT {migrator} TO CURRENT_USER")
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {migrator}")
    await conn.execute(f"ALTER SCHEMA {schema} OWNER TO {migrator}")
    await conn.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
    api_roles = await conn.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])", list(SUPABASE_API_ROLES)
    )
    for row in api_roles:
        role = f'"{row["rolname"]}"'
        await conn.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {role}")
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
    await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {app}")
    defaults = f"ALTER DEFAULT PRIVILEGES FOR ROLE {migrator} IN SCHEMA {schema}"
    await conn.execute(f"{defaults} REVOKE ALL ON TABLES FROM PUBLIC")
    await conn.execute(f"{defaults} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app}")
    await conn.execute(f"{defaults} GRANT USAGE, SELECT ON SEQUENCES TO {app}")
    await conn.execute(f"{defaults} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    await conn.execute(f"{defaults} GRANT EXECUTE ON FUNCTIONS TO {app}")
    await conn.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {app}"
    )
    await conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {app}")
    # The migration table is the migrator's alone.
    if await conn.fetchval("SELECT to_regclass('gize.alembic_version') IS NOT NULL"):
        await conn.execute(f"REVOKE ALL ON {schema}.alembic_version FROM {app}")


def _quote(value: str) -> str:
    """A setting value from the constants above, as an SQL literal."""
    if not value.replace(" ", "").isalnum():
        raise BootstrapError("unexpected setting value")
    return f"'{value}'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--create-supabase-roles",
        action="store_true",
        help="create stand-ins for Supabase's API roles (local and test databases only)",
    )
    args = parser.parse_args()
    names = ("BOOTSTRAP_ADMIN_DATABASE_URL", "DATABASE_URL", "MIGRATOR_DATABASE_URL")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print(f"bootstrap: set {', '.join(missing)}", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    try:
        asyncio.run(
            bootstrap(
                os.environ["BOOTSTRAP_ADMIN_DATABASE_URL"],
                os.environ["DATABASE_URL"],
                os.environ["MIGRATOR_DATABASE_URL"],
                create_supabase_roles=args.create_supabase_roles,
            )
        )
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    print("bootstrap: done", file=sys.stderr)  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":
    sys.exit(main())
