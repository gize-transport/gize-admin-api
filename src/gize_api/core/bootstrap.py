"""One-off database bootstrap: roles, schema, grants. Run by a human, safe to run again.

    uv run --env-file .env python -m gize_api.core.bootstrap [--create-supabase-roles]

Reads BOOTSTRAP_ADMIN_DATABASE_URL (the `postgres` role), DATABASE_URL (gize_app) and
MIGRATOR_DATABASE_URL (gize_migrator); role passwords are taken from the last two, so they
can never disagree. The admin connection to any server but this machine must use
`ssl=verify-full`, against DATABASE_SSL_ROOT_CERT when set. `--create-supabase-roles`
creates stand-ins for Supabase's `anon`, `authenticated` and `service_role` (local and
test databases only).

Result:
- `gize_migrator` owns schema `gize` and runs migrations. Through default privileges,
  `gize_app` gets SELECT, INSERT, UPDATE, DELETE on every table the migrator creates, and
  EXECUTE on its functions; no one else gets anything (PUBLIC included). After that,
  migrations own table grants: re-running the bootstrap never re-grants on existing tables.
- Neither role is superuser, creates roles or databases, replicates, bypasses RLS, is a
  member of another role, or has a password that expires.
- Supabase's API roles and PUBLIC have no access to the schema.
- Role settings: gize_app statement 5 s, lock 3 s, idle in transaction 10 s, search_path
  `gize`; gize_migrator lock 5 s, statement 5 min.

Re-running resets the roles' settings (role-wide and for this database) and checks for
drift, stopping and naming it rather than accepting it: memberships either way (our roles
in others, or others in ours), settings for other databases, CREATE on schemas `gize` and
`public` or the database, TRUNCATE/REFERENCES/TRIGGER/MAINTAIN on a table, or objects
owned by gize_app.

The admin is made a member of gize_migrator only for the run (to create the schema for it
and set its default privileges); the membership is revoked at the end. Postgres 16+ leaves
a CREATEROLE admin ADMIN OPTION on roles it created; that lets it manage the roles, not act
as them. Objects created in `gize` by hand (the dashboard) are owned by the admin and get
neither the default privileges nor RLS: change the schema only through migrations.

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
import ssl
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit

import asyncpg

from gize_api.core.config import database_ssl_mode, is_database_url
from gize_api.core.db import ssl_argument

APP_ROLE = "gize_app"
MIGRATOR_ROLE = "gize_migrator"
SCHEMA = "gize"
SUPABASE_API_ROLES = ("anon", "authenticated", "service_role")
# Supabase's service_role bypasses RLS; its local stand-in must too, or tests overstate RLS.
_BYPASS_RLS_ROLES = frozenset({"service_role"})
SCRAM_ITERATIONS = 4096
# How long the admin waits for a lock held by someone else before giving up.
ADMIN_LOCK_TIMEOUT = "5s"
ADMIN_STATEMENT_TIMEOUT = "1min"

_ROLE_SETTINGS = {
    APP_ROLE: {
        "statement_timeout": "5s",
        "lock_timeout": "3s",
        "idle_in_transaction_session_timeout": "10s",
        "search_path": SCHEMA,
    },
    # The migrator keeps the default search_path: Alembic compares `gize` by name, which
    # breaks if `gize` is the connection's default schema (gize_api.core.migrations).
    MIGRATOR_ROLE: {"lock_timeout": "5s", "statement_timeout": "5min"},
}
# Table privileges beyond data access; gize_app must hold none of them on any object.
_FORBIDDEN_TABLE_PRIVILEGES = ("TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")
# Postgres 17, as on Supabase: the checks use MAINTAIN and membership options (16+).
MIN_SERVER_VERSION = 170000
# Schemas where gize_app must not create anything.
_NO_CREATE_SCHEMAS = (SCHEMA, "public")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
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
    admin_url: str,
    app_url: str,
    migrator_url: str,
    *,
    create_supabase_roles: bool,
    ssl_root_cert: Path | None = None,
) -> None:
    """Create or update roles, schema and grants in the database `admin_url` points at."""
    passwords = {
        APP_ROLE: _password(app_url, APP_ROLE, "DATABASE_URL"),
        MIGRATOR_ROLE: _password(migrator_url, MIGRATOR_ROLE, "MIGRATOR_DATABASE_URL"),
    }
    dsn, ssl_argument = _admin_connection_arguments(admin_url, ssl_root_cert)
    conn = await asyncpg.connect(dsn, timeout=10, ssl=ssl_argument)
    try:
        await conn.execute(f"SET lock_timeout = {_quote(ADMIN_LOCK_TIMEOUT)}")
        await conn.execute(f"SET statement_timeout = {_quote(ADMIN_STATEMENT_TIMEOUT)}")
        version = int(await conn.fetchval("SHOW server_version_num"))
        if version < MIN_SERVER_VERSION:
            raise BootstrapError(f"needs Postgres {MIN_SERVER_VERSION // 10000} or later")
        async with conn.transaction():
            if create_supabase_roles:
                for role in SUPABASE_API_ROLES:
                    await _create_role(conn, role, login=False)
                    if role in _BYPASS_RLS_ROLES:
                        await conn.execute(f'ALTER ROLE "{role}" BYPASSRLS')
            for role, password in passwords.items():
                await _create_role(conn, role, login=True)
                # With createrole_self_grant set, creating a role also lets its creator act
                # as it; keep only ADMIN OPTION (another grantor's), which manages it.
                await conn.execute(f'REVOKE "{role}" FROM CURRENT_USER')
                await _set_password(conn, role, password)
                await _check_attributes(conn, role)
                await _reset_settings(conn, role)
                for name, value in _ROLE_SETTINGS[role].items():
                    await conn.execute(f'ALTER ROLE "{role}" SET {name} = {_quote(value)}')
            await conn.execute(f'ALTER ROLE "{APP_ROLE}" NOINHERIT')
            await _grant_schema(conn)
            await _check_members(conn)
            await _check_app_privileges(conn)
    except asyncpg.exceptions.LockNotAvailableError:
        raise BootstrapError(
            "waited too long for a lock held by another session; try again later"
        ) from None
    finally:
        await conn.close()


def _admin_connection_arguments(
    url: str, ssl_root_cert: Path | None
) -> tuple[str, ssl.SSLContext | Literal["require"] | None]:
    """asyncpg's DSN (without our `ssl` option) and `ssl` argument for the admin URL.

    The admin can create roles and rewrite grants: off this machine, its session must be
    verified (`ssl=verify-full`), never merely encrypted or left to asyncpg's default.
    """
    if not is_database_url(url):
        raise BootstrapError(
            "BOOTSTRAP_ADMIN_DATABASE_URL must be"
            " postgresql://user:password@host[:port]/database[?ssl=verify-full]"
        )
    parts = urlsplit(url)
    mode = database_ssl_mode(url)
    if parts.hostname not in _LOOPBACK_HOSTS and mode != "verify-full":
        raise BootstrapError(
            "BOOTSTRAP_ADMIN_DATABASE_URL must set ssl=verify-full off this machine"
        )
    try:
        argument = ssl_argument(url, ssl_root_cert)
    except (OSError, ssl.SSLError):
        raise BootstrapError("DATABASE_SSL_ROOT_CERT must be a readable CA certificate") from None
    return urlunsplit(parts._replace(query="")), argument


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
        "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L VALID UNTIL %L',"
        " $1::text, $2::text, 'infinity')",
        role,
        scram_verifier(password),
    )
    await conn.execute(statement)


async def _check_attributes(conn: asyncpg.Connection, role: str) -> None:
    row = await conn.fetchrow("SELECT * FROM pg_roles WHERE rolname = $1", role)
    granted = [name for name in _FORBIDDEN_ATTRIBUTES if row is not None and row[name]]
    if granted:
        raise BootstrapError(f"{role} must not have {', '.join(granted)}; fix it by hand first")
    memberships = await conn.fetch(
        "SELECT roleid::regrole::text AS name FROM pg_auth_members"
        " WHERE member = $1::regrole ORDER BY 1",
        role,
    )
    if memberships:
        names = ", ".join(row["name"] for row in memberships)
        raise BootstrapError(f"{role} must not be a member of {names}; fix it by hand first")


async def _reset_settings(conn: asyncpg.Connection, role: str) -> None:
    """Drop the role's settings, role-wide and for this database (those take precedence; a
    role may set its own), and refuse settings for other databases on the server."""
    database = await conn.fetchval("SELECT format('%I', current_database())")
    await conn.execute(f'ALTER ROLE "{role}" RESET ALL')
    await conn.execute(f'ALTER ROLE "{role}" IN DATABASE {database} RESET ALL')
    other = await conn.fetchval(
        "SELECT d.datname FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase"
        " WHERE s.setrole = $1::regrole AND d.datname <> current_database() LIMIT 1",
        role,
    )
    if other is not None:
        raise BootstrapError(
            f"{role} has settings for another database ({other}); remove them with"
            f" ALTER ROLE {role} IN DATABASE <name> RESET ALL"
        )


async def _check_members(conn: asyncpg.Connection) -> None:
    """No role may act as ours. The admin may keep ADMIN OPTION only (Postgres 16+ gives a
    CREATEROLE role that for roles it creates): it manages the roles without acting as them.
    """
    member = await conn.fetchrow(
        "SELECT m.member::regrole::text AS member, m.roleid::regrole::text AS role"
        " FROM pg_auth_members m WHERE m.roleid IN ($1::regrole, $2::regrole)"
        " AND NOT (m.member = current_user::regrole AND NOT m.set_option"
        " AND NOT m.inherit_option) ORDER BY 1, 2 LIMIT 1",
        APP_ROLE,
        MIGRATOR_ROLE,
    )
    if member is not None:
        raise BootstrapError(
            f"{member['member']} must not be a member of {member['role']}"
            " (ADMIN OPTION alone is allowed for the admin); fix it by hand first"
        )


async def _check_app_privileges(conn: asyncpg.Connection) -> None:
    """Fail on anything that lets gize_app change structure; the run is rolled back."""
    for schema in _NO_CREATE_SCHEMAS:
        if await conn.fetchval(
            "SELECT to_regnamespace($2) IS NOT NULL AND has_schema_privilege($1, $2, 'CREATE')",
            APP_ROLE,
            schema,
        ):
            raise BootstrapError(f"{APP_ROLE} must not have CREATE on schema {schema}")
    if await conn.fetchval(
        "SELECT has_database_privilege($1, current_database(), 'CREATE')", APP_ROLE
    ):
        raise BootstrapError(f"{APP_ROLE} must not have CREATE on the database")
    owned = await conn.fetchval(
        "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relowner = $1::regrole"
        " UNION ALL SELECT format('%I', nspname) FROM pg_namespace WHERE nspowner = $1::regrole"
        " UNION ALL SELECT oid::regprocedure::text FROM pg_proc WHERE proowner = $1::regrole"
        " LIMIT 1",
        APP_ROLE,
    )
    if owned is not None:
        raise BootstrapError(f"{APP_ROLE} must not own {owned}")
    extra = await conn.fetchrow(
        "SELECT format('%I.%I', n.nspname, c.relname) AS name, p.privilege FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " CROSS JOIN unnest($3::text[]) AS p(privilege)"
        " WHERE n.nspname = $2 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
        " AND has_table_privilege($1, c.oid, p.privilege) LIMIT 1",
        APP_ROLE,
        SCHEMA,
        list(_FORBIDDEN_TABLE_PRIVILEGES),
    )
    if extra is not None:
        raise BootstrapError(f"{APP_ROLE} must not have {extra['privilege']} on {extra['name']}")


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
    # Postgres lets PUBLIC execute every new function. That default is global, so it is
    # revoked for all of the migrator's functions; a per-schema revoke would do nothing.
    await conn.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {migrator} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    defaults = f"ALTER DEFAULT PRIVILEGES FOR ROLE {migrator} IN SCHEMA {schema}"
    await conn.execute(f"{defaults} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app}")
    await conn.execute(f"{defaults} GRANT USAGE, SELECT ON SEQUENCES TO {app}")
    await conn.execute(f"{defaults} GRANT EXECUTE ON FUNCTIONS TO {app}")
    await conn.execute(f"REVOKE {migrator} FROM CURRENT_USER")


def _quote(value: str) -> str:
    """A setting value from the constants above, as an SQL literal."""
    if not value.replace(" ", "").replace("_", "").isalnum():
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
                ssl_root_cert=Path(root)
                if (root := os.environ.get("DATABASE_SSL_ROOT_CERT"))
                else None,
            )
        )
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    print("bootstrap: done", file=sys.stderr)  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":
    sys.exit(main())
