"""Database access: one async engine per process, a session per request.

The app connects as `gize_app` (DML only, see core/bootstrap.py). Every connection has a
3 s connect timeout and a 5 s statement timeout; waiting for a pooled connection is capped at
3 s. Statement parameters never appear in error text (`hide_parameters`). Services own
transactions; a database that is down, slow, saturated or asking for a retry becomes 503
SERVICE_UNAVAILABLE with Retry-After (core/errors.py).

The session rule: every use of a session is inside `async with session.begin():`, one block
per unit of work. Autobegin is off, so a query outside a block fails at once (but
`session.add()` does not: add objects inside the block that should save them). A dependency
(auth) uses its own short block, finished before the service opens its own, and hands on
plain values (IDs, role), not ORM objects. `current_staff`'s block reads, and once per
member writes `first_signed_in_at`. Every ORM query and `get()` refreshes
rows the session already holds (GizeSession), so a later block never sees stale data; role and
ownership checks still belong in the service's block, next to the write they protect.
Never hold a block open across an outbound call (SMS, HTTP).

Connect directly or through Supabase's session-mode pooler (port 5432). Transaction mode
(port 6543) is not supported: asyncpg and SQLAlchemy keep prepared statements per server
connection, which transaction mode shares between clients.
"""

import asyncio
import contextvars
import dataclasses
import ssl
import traceback
import weakref
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import FrameType
from typing import Any, Literal

import sqlalchemy.exc
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction

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


def create_engine(
    settings: Settings, *, pool_size: int | None = None, max_overflow: int | None = None
) -> AsyncEngine:
    """The app's engine (pool from settings unless given). No connection until first use."""
    return _create_async_engine(
        async_url(settings.database_url.get_secret_value()),
        pool_size=settings.database_pool_size if pool_size is None else pool_size,
        max_overflow=settings.database_max_overflow if max_overflow is None else max_overflow,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=_connect_args(settings),
    )


class GizeSession(Session):
    """The ORM session for the app: ORM queries and `get()` refresh rows it already holds.

    A request's dependency and handler share one session; without this, a row loaded in the
    dependency's block (a staff member's role) would be served from the session's cache in
    the service's block, even after another request changed it. Inside `no_autoflush`
    nothing is refreshed: unsaved edits would otherwise be overwritten.
    """

    # Forwards Session.get unchanged except for the default; callers are typed through
    # AsyncSession.get.
    def get(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if kwargs.get("populate_existing") is None:
            kwargs["populate_existing"] = self.autoflush
        return super().get(*args, **kwargs)


# Open GizeSession transactions, for the outbound HTTP and enqueue guards.
#
# Each task that opens a transaction gets a live set of its open transactions, appended to
# a chain in its context. Child tasks (create_task, gather, TaskGroup) copy the context and
# so see their ancestors' live sets: inside the parent's block they are caught, and once it
# commits they are free. A child's own transactions go into a set of its own, which the
# parent never sees. A transaction closed in another task is removed from the set it was
# opened in. Raw engine connections are not tracked: app code uses sessions.
@dataclasses.dataclass(eq=False)
class _OpenTransactions:
    task: "asyncio.Task[Any] | None"
    ids: set[int] = dataclasses.field(default_factory=set)


_open_chain: contextvars.ContextVar[tuple[_OpenTransactions, ...]] = contextvars.ContextVar(
    "gize_open_transactions", default=()
)
_opened_in: "weakref.WeakKeyDictionary[SessionTransaction, _OpenTransactions]" = (
    weakref.WeakKeyDictionary()
)


class OpenTransactionError(RuntimeError):
    """A programming error: an outside call or enqueue inside a database transaction."""


def database_transaction_open() -> bool:
    """True inside `async with session.begin():` (any GizeSession), in this task or a task
    that started this one."""
    return any(entry.ids for entry in _open_chain.get())


def _current_task() -> "asyncio.Task[Any] | None":
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running event loop (sync code)
        return None


@event.listens_for(GizeSession, "after_transaction_create")
def _transaction_opened(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is not None:
        return
    chain, task = _open_chain.get(), _current_task()
    if chain and chain[-1].task is task:
        entry = chain[-1]
    else:
        entry = _OpenTransactions(task)
        _open_chain.set((*chain, entry))
    entry.ids.add(id(transaction))
    _opened_in[transaction] = entry


@event.listens_for(GizeSession, "after_transaction_end")
def _transaction_closed(session: Session, transaction: SessionTransaction) -> None:
    entry = _opened_in.pop(transaction, None)
    if entry is not None:
        entry.ids.discard(id(transaction))


@event.listens_for(GizeSession, "do_orm_execute")
def _refresh_loaded_rows(state: ORMExecuteState) -> None:
    if (
        state.session.autoflush  # pending edits were just flushed, so nothing is lost
        and state.is_select
        and not state.is_column_load
        and not state.is_relationship_load
    ):
        state.update_execution_options(populate_existing=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions without autobegin that re-read rows: see the session rule above."""
    return async_sessionmaker(
        engine, expire_on_commit=False, autobegin=False, sync_session_class=GizeSession
    )


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
    # Network failures surface as plain OS and timeout errors. Count one only if the network
    # stack or driver raised it (not app code run inside the ORM) on the way through the
    # database driver (not an outbound call or Redis failing in the same request).
    return (
        isinstance(exc, (OSError, TimeoutError))
        and any(_raise_site(error).startswith(_NETWORK_MODULES) for error in _chain(exc))
        and _raised_from(exc, ("asyncpg.", "sqlalchemy."))
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


UNIQUE_VIOLATION = "23505"
LIKE_ESCAPE = "/"


def escape_like(text: str) -> str:
    """`text` with LIKE wildcards as plain characters, for `.startswith(..., escape=LIKE_ESCAPE)`:
    a "%" someone typed matches a percent sign."""
    for char in (LIKE_ESCAPE, "%", "_"):
        text = text.replace(char, LIKE_ESCAPE + char)
    return text


def violated_unique_constraint(exc: BaseException) -> str | None:
    """The name of the unique constraint or index `exc` broke, if it is a unique violation.

    Services pre-check uniqueness for a clear error; this catches the race in which two
    requests both passed the check (the database's index decides).
    """
    if database_sqlstate(exc) != UNIQUE_VIOLATION:
        return None
    driver_error = getattr(getattr(exc, "orig", None), "__cause__", None)
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None


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
