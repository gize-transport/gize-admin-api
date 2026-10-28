"""Database access: one async engine per process, a session per request.

The app connects as `gize_app` (DML only, see core/bootstrap.py). Every connection has a
3 s connect timeout and a 5 s statement timeout; waiting for a pooled connection is capped at
3 s. Statement parameters never appear in error text (`hide_parameters`). Services own
transactions (`async with session.begin()`); a database that is down, slow or out of
connections becomes 503 SERVICE_UNAVAILABLE (core/errors.py).
"""

import ssl
import traceback
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import sqlalchemy.exc
from fastapi import Request
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from gize_api.core.config import Settings

CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000
POOL_SIZE = 10
POOL_MAX_OVERFLOW = 10
POOL_TIMEOUT_SECONDS = 3
POOL_RECYCLE_SECONDS = 1800
APPLICATION_NAME = "gize-api"

# SQLSTATE classes and codes that mean "the database is unavailable", not "the query is wrong":
# connection exceptions (08), query cancelled by statement timeout (57014), lock timeout
# (55P03), server shutting down or not accepting connections (57P0x), too many connections.
_UNAVAILABLE_SQLSTATE_PREFIXES = ("08", "57P")
_UNAVAILABLE_SQLSTATES = frozenset({"57014", "55P03", "53300"})


def async_url(url: str) -> URL:
    """A SQLAlchemy asyncpg URL, without the `ssl` option (passed to the driver instead)."""
    parsed = make_url(url).set(drivername="postgresql+asyncpg")
    return parsed.difference_update_query(["ssl"])


def create_engine(settings: Settings) -> AsyncEngine:
    """The app's engine. No connection is made until first use."""
    return _create_async_engine(
        async_url(settings.database_url.get_secret_value()),
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=_connect_args(settings),
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a session for this request, closed afterwards."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


def is_database_unavailable(exc: BaseException) -> bool:
    """True when `exc` means the database is down, slow or saturated (503), not a bug."""
    if isinstance(exc, sqlalchemy.exc.TimeoutError):  # waiting for a pooled connection
        return True
    if isinstance(exc, sqlalchemy.exc.DBAPIError):
        if exc.connection_invalidated:
            return True
        sqlstate = str(getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", ""))
        return sqlstate in _UNAVAILABLE_SQLSTATES or sqlstate.startswith(
            _UNAVAILABLE_SQLSTATE_PREFIXES
        )
    # Connecting raises plain OS and timeout errors; only count those from the driver.
    return isinstance(exc, (OSError, TimeoutError)) and _raised_by_database_driver(exc)


def _raised_by_database_driver(exc: BaseException) -> bool:
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith(("sqlalchemy.", "asyncpg.")):
            return True
    return False


def _connect_args(settings: Settings) -> dict[str, object]:
    args: dict[str, object] = {
        "timeout": CONNECT_TIMEOUT_SECONDS,
        # A client-side cap just above the server's statement timeout, for a hung server.
        "command_timeout": STATEMENT_TIMEOUT_MS / 1000 + 1,
        # Also set on the role (bootstrap); repeated here so a pooler cannot drop them.
        "server_settings": {
            "application_name": APPLICATION_NAME,
            "statement_timeout": str(STATEMENT_TIMEOUT_MS),
            "lock_timeout": str(LOCK_TIMEOUT_MS),
            "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
        },
        # Safe with Supabase's transaction-mode pooler as well as session mode.
        "statement_cache_size": 0,
    }
    args.update(ssl_connect_argument(settings.database_url.get_secret_value()))
    return args


def ssl_connect_argument(url: str) -> dict[str, object]:
    """asyncpg's `ssl` argument for the URL's `ssl` option (none when the option is absent).

    `verify-full` checks the certificate and host name; `require` encrypts without checking.
    """
    mode = parse_qs(urlsplit(url).query).get("ssl", [None])[0]
    if mode == "verify-full":
        return {"ssl": ssl.create_default_context()}
    if mode == "require":
        return {"ssl": "require"}
    return {}
