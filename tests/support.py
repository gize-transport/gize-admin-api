"""Shared test types and constants (import from here, not from conftest)."""

import base64
import contextlib
import dataclasses
import fcntl
import json
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config

from gize_api.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent

# Ports nothing listens on: calls fail fast and nothing real is touched.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"
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
    host = socket.gethostbyname(parts.hostname or "127.0.0.1")
    return Path(tempfile.gettempdir()) / f"gize-test-postgres-{host}_{parts.port or 5432}.lock"


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
