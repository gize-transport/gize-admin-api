"""Shared test types and constants (import from here, not from conftest)."""

import asyncio
import base64
import contextlib
import dataclasses
import fcntl
import ipaddress
import json
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from gize_api.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent

# Ports nothing listens on: calls fail fast and nothing real is touched.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"
# Signs verified-phone tokens in tests (GZ-031): 32 bytes, as the setting requires.
TEST_VERIFIED_PHONE_TOKEN_KEY = "test-verified-phone-key-0123456789abcdef"  # noqa: S105
TEST_SUPABASE_URL = "https://abcdefghijklmnopqrst.supabase.co"
UNREACHABLE_DATABASE_URL = "postgresql://gize_app:unused@127.0.0.1:1/gize?ssl=verify-full"

# Every variable Settings reads; cleared so the developer's shell cannot change a result.
SETTINGS_ENV_VARS = (
    "APP_ENV",
    "REDIS_URL",
    "DATABASE_URL",
    "CORS_ALLOWED_ORIGINS",
    "LOG_LEVEL",
    "SENTRY_DSN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "RELEASE",
    "OBSERVABILITY_REQUIRED",
    "OTEL_TRACES_SAMPLER_RATIO",
    "DATABASE_SSL_ROOT_CERT",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "CLIENT_IP_HEADER",
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
    "VERIFIED_PHONE_TOKEN_KEY",
    "AFROMESSAGE_TOKEN",
    "AFROMESSAGE_SENDER_NAME",
    "AFROMESSAGE_IDENTIFIER_ID",
)

SettingsFactory = Callable[..., Settings]


def fake_jwt(subject: str = "test") -> str:
    """A syntactically valid, unsigned-looking JWT built at runtime.

    Tests need token-shaped strings to prove scrubbing; building them here keeps secret
    scanners (rightly) failing on any token literal committed to the repo.
    """

    def part(data: dict[str, str]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'sub': subject})}.c2lnbmF0dXJl"


@dataclasses.dataclass(frozen=True)
class DatabaseUrls:
    """Connection URLs for one test database: admin (superuser), migrator and app."""

    name: str

    admin: str
    migrator: str
    app: str


async def wait_for_lock_waiters(database: DatabaseUrls, count: int = 1) -> None:
    """Poll Postgres until `count` backends wait on a lock: requests are blocked where a
    test wants them, with no timing guess."""
    admin = await asyncpg.connect(database.admin, timeout=3)
    try:
        for _ in range(500):
            waiting = await admin.fetchval(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = $1 AND wait_event_type = 'Lock'",
                database.name,
            )
            if waiting >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected {count} backend(s) waiting on a lock")
    finally:
        await admin.close()


# Passwords for the throwaway test roles only; the real ones come from the URLs in .env.
TEST_APP_PASSWORD = "gize-app-test"  # noqa: S105 - local test role
TEST_MIGRATOR_PASSWORD = "gize-migrator-test"  # noqa: S105 - local test role


def database_urls(admin_url: str, database: str) -> DatabaseUrls:
    """URLs for `database` on the server behind `admin_url` (a superuser URL)."""
    parts = urlsplit(admin_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432

    def url(user: str, password: str) -> str:
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    admin = urlunsplit(parts._replace(path=f"/{database}"))
    return DatabaseUrls(
        name=database,
        admin=admin,
        migrator=url("gize_migrator", TEST_MIGRATOR_PASSWORD),
        app=url("gize_app", TEST_APP_PASSWORD),
    )


def alembic_config(migrator_url: str, **attributes: object) -> Config:
    """The release step's Alembic configuration, pointed at `migrator_url`."""
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes.update({"database_url": migrator_url, "app_env": "test", **attributes})
    return config


def run_migrations(migrator_url: str, target: str, *, downgrade: bool = False) -> None:
    """Run Alembic exactly as the release step does, against `migrator_url`."""
    config = alembic_config(migrator_url)
    if downgrade:
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)


TEST_SERVER_LOCK_TIMEOUT_SECONDS = 600.0


def server_lock_path(admin_url: str) -> Path:
    """One lock file per test server, whatever name the URL gives the host."""
    parts = urlsplit(admin_url)
    port = parts.port or 5432
    address = ipaddress.ip_address(socket.getaddrinfo(parts.hostname or "::1", port)[0][4][0])
    safe_host = "loopback" if address.is_loopback else str(address).replace(":", "-")
    return Path(tempfile.gettempdir()) / f"gize-test-postgres-{safe_host}_{port}.lock"


@contextlib.contextmanager
def hold_server_lock(
    path: Path, timeout_seconds: float = TEST_SERVER_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold the lock, waiting for another run to finish (bounded, and saying so)."""
    with path.open("w") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        announced = False
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"another test run has held {path} for {timeout_seconds:.0f} s;"
                        " wait for it to finish or stop it"
                    )
                if not announced:
                    print(f"waiting for another test run to release {path}", file=sys.__stderr__)
                    announced = True
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


class StallableProxy:
    """A TCP proxy in front of a real server that can freeze mid-stream (a hung peer).

    Unlike a listener that never answers, it lets connections open and work first, so
    tests can reproduce a server that hangs on an already-established connection.
    """

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    def freeze(self) -> None:
        self._flowing.clear()

    async def close(self) -> None:
        self._flowing.set()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        await self._flowing.wait()
        server_reader, server_writer = await asyncio.open_connection(*self._target)
        self._writers += [client_writer, server_writer]
        await asyncio.gather(
            self._pump(client_reader, server_writer), self._pump(server_reader, client_writer)
        )

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            while data := await reader.read(65536):
                await self._flowing.wait()
                writer.write(data)
                await writer.drain()
        writer.close()


async def public_resolver(host: str, port: int) -> list[str]:
    """Resolves every host to a public address: outbound-client tests on a mock transport."""
    return ["93.184.216.34"]


def memory_log_exporter() -> InMemoryLogRecordExporter:
    """The SDK's in-memory log exporter (GZ-066), whose constructor is untyped."""
    return InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]


async def every_page(
    api: httpx.AsyncClient, path: str, headers: dict[str, str], **params: str
) -> list[str]:
    """Every item's id in a list, following `next_cursor` two at a time (GZ-081): the
    order across pages, failing on a repeat or an endless walk."""
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        query = {**params, "limit": "2", **({"cursor": cursor} if cursor else {})}
        response = await api.get(path, params=query, headers=headers)
        assert response.status_code == 200, response.json()
        ids += [item["id"] for item in response.json()["items"]]
        cursor = response.json()["next_cursor"]
        if cursor is None:
            assert len(ids) == len(set(ids)), ids
            return ids
    raise AssertionError("the pages never ended")
