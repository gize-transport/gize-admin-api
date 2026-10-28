"""Database access: one async engine per process, a session per request.

The app connects as `gize_app` (DML only, see core/bootstrap.py). Every connection has a
3 s connect timeout and a 5 s statement timeout; waiting for a pooled connection is capped at
3 s. Statement parameters never appear in error text (`hide_parameters`). Services own
transactions (`async with session.begin()`); a database that is down, slow, saturated or
asking for a retry becomes 503 SERVICE_UNAVAILABLE with Retry-After (core/errors.py).

Connect directly or through Supabase's session-mode pooler (port 5432). Transaction mode
(port 6543) is not supported: asyncpg and SQLAlchemy keep prepared statements per server
connection, which transaction mode shares between clients.
"""

import ssl
import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import FrameType
from typing import Literal

import sqlalchemy.exc
from fastapi import Request
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from gize_api.core.config import Settings, database_ssl_mode

CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000
POOL_TIMEOUT_SECONDS = 3
POOL_RECYCLE_SECONDS = 1800
APPLICATION_NAME = "gize-api"

# SQLSTATE classes and codes that mean "unavailable, try again", not "the query is wrong":
# connection exceptions (08), insufficient resources such as too many connections, out of
# memory or disk full (53), server shutting down (57P0x), statement timeout (57014), lock
# timeout (55P03), and a transaction that lost a race: serialization failure (40001) or
# deadlock (40P01). Services that can safely retry those may do so before this applies.
_UNAVAILABLE_SQLSTATE_PREFIXES = ("08", "53", "57P")
_UNAVAILABLE_SQLSTATES = frozenset({"57014", "55P03", "40001", "40P01"})
# Driver modules whose frames mean the error happened while opening a connection.
_CONNECTING_MODULES = ("asyncpg.connect_utils",)
# Where network failures are raised. An OS or timeout error raised anywhere else (app code
# run inside the ORM, e.g. a validator) is a bug, even though SQLAlchemy frames surround it.
_NETWORK_MODULES = ("asyncio.", "asyncpg.", "socket", "ssl", "uvloop")
# Server answers that will not fix themselves: wrong password or role (28), no such
# database (3D000). Still 503 for the client, but reported for a human to fix.
_MISCONFIGURED_SQLSTATE_PREFIXES = ("28", "3D000")
_MAX_CHAINED_ERRORS = 10


def async_url(url: str) -> URL:
    """A SQLAlchemy asyncpg URL, without the `ssl` option (passed to the driver instead)."""
    parsed = make_url(url).set(drivername="postgresql+asyncpg")
    return parsed.difference_update_query(["ssl"])


def create_engine(settings: Settings) -> AsyncEngine:
    """The app's engine. No connection is made until first use."""
    return _create_async_engine(
        async_url(settings.database_url.get_secret_value()),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
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
    """True when `exc` means the database is down, slow, saturated or asks for a retry (503).

    Anything the server says while a connection is being opened counts (a wrong password
    after a rotation, a pooler at its client limit): no statement of ours ran.
    """
    if isinstance(exc, sqlalchemy.exc.TimeoutError):  # waiting for a pooled connection
        return True
    if isinstance(exc, sqlalchemy.exc.DBAPIError):
        if exc.connection_invalidated or _raised_from(exc, _CONNECTING_MODULES):
            return True
        sqlstate = database_sqlstate(exc) or ""
        return sqlstate in _UNAVAILABLE_SQLSTATES or sqlstate.startswith(
            _UNAVAILABLE_SQLSTATE_PREFIXES
        )
    # Network failures surface as plain OS and timeout errors; count only those raised by
    # the network stack or the driver themselves.
    return isinstance(exc, (OSError, TimeoutError)) and any(
        _raise_site(error).startswith(_NETWORK_MODULES) for error in _chain(exc)
    )


def is_database_misconfigured(exc: BaseException) -> bool:
    """True for unavailability that needs a human: credentials, database name, TLS trust."""
    sqlstate = database_sqlstate(exc) or ""
    if sqlstate.startswith(_MISCONFIGURED_SQLSTATE_PREFIXES):
        return True
    return any(isinstance(error, ssl.SSLCertVerificationError) for error in _chain(exc))


def database_sqlstate(exc: BaseException) -> str | None:
    """The five-character SQLSTATE the server sent, if any (a code, never message text)."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate if isinstance(sqlstate, str) and len(sqlstate) == 5 else None


def _raised_from(exc: BaseException, modules: tuple[str, ...]) -> bool:
    """True if a frame of `exc` or the errors it wraps belongs to one of `modules`."""
    return any(
        _module(frame).startswith(modules)
        for error in _chain(exc)
        for frame, _ in traceback.walk_tb(error.__traceback__)
    )


def _raise_site(exc: BaseException) -> str:
    """The module of the innermost frame: where `exc` was raised."""
    frames = list(traceback.walk_tb(exc.__traceback__))
    return _module(frames[-1][0]) if frames else ""


def _module(frame: FrameType) -> str:
    return str(frame.f_globals.get("__name__", ""))


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """`exc` and the errors it wraps (cause or context), bounded."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAINED_ERRORS:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


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
    }
    args.update(
        ssl_connect_argument(
            settings.database_url.get_secret_value(), settings.database_ssl_root_cert
        )
    )
    return args


def ssl_connect_argument(url: str, root_cert: Path | None = None) -> dict[str, object]:
    """asyncpg's `ssl` argument for the URL's `ssl` option (none when the option is absent).

    `verify-full` checks the certificate chain (against `root_cert` alone when given,
    otherwise the system CAs) and the host name; `require` encrypts without checking.
    """
    argument = ssl_argument(url, root_cert)
    return {} if argument is None else {"ssl": argument}


def ssl_argument(
    url: str, root_cert: Path | None = None
) -> ssl.SSLContext | Literal["require"] | None:
    """The value of asyncpg's `ssl` argument for the URL (see ssl_connect_argument)."""
    mode = database_ssl_mode(url)
    if mode == "verify-full":
        return ssl.create_default_context(cafile=root_cert)
    if mode == "require":
        return "require"
    return None
