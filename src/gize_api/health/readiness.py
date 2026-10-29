"""Readiness: can this instance serve requests right now?

Readiness depends on Postgres only. Redis is checked too, and a failure is logged at ERROR
and counted, but never decides the answer: every instance shares one Redis, so failing
readiness on it would take the whole API offline (engineering-standards.md, "Health").

- **Own connections.** The checks use a Postgres connection and a Redis connection of their
  own, never the app's pools: at peak every request may hold a pooled connection, and a
  healthy but busy instance must not look unready. They are not traced.
- **Bounded.** Each check runs as a background task bounded by READINESS_TIMEOUT_SECONDS.
  A probe waits for the Postgres task at most that long and otherwise answers "not ready"
  while the task finishes (cancelling a hung connection can take longer than the timeout
  itself). It never waits for the Redis task, which runs alongside and only reports.
- **Shared.** The endpoint is public: concurrent probes share the running task, and one
  result serves every probe for READINESS_CACHE_SECONDS.
- **Quiet.** An outage is logged when it starts, every OUTAGE_REMINDER_SECONDS while it
  lasts, and when it ends; every failed check is counted.
"""

import asyncio
import contextlib
import contextvars
import time

import structlog
from redis.asyncio import Redis
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncEngine

from gize_api.core.config import Settings
from gize_api.core.db import create_engine
from gize_api.core.redis_options import connection_options
from gize_api.core.telemetry import Telemetry

READINESS_TIMEOUT_SECONDS = 1.0
READINESS_CACHE_SECONDS = 1.0
OUTAGE_REMINDER_SECONDS = 60.0
REDIS_CLIENT_NAME = "gize-api-health"

logger = structlog.get_logger("gize_api.health")


class Readiness:
    def __init__(self, engine: AsyncEngine, redis: Redis, telemetry: Telemetry | None) -> None:
        self._engine = engine
        self._redis = redis
        self._telemetry = telemetry
        self._check: asyncio.Task[bool] | None = None
        self._redis_check: asyncio.Task[bool] | None = None
        self._ready = False
        self._checked_at: float | None = None
        self._outage_logged_at: dict[str, float] = {}

    @classmethod
    def for_settings(cls, settings: Settings, telemetry: Telemetry | None) -> "Readiness":
        """With one Postgres and one Redis connection of its own (each instance's budget)."""
        engine = create_engine(settings, pool_size=1, max_overflow=0)
        url = settings.redis_url.get_secret_value()
        redis = Redis.from_url(
            url,
            **connection_options(
                url,
                timeout=READINESS_TIMEOUT_SECONDS,
                max_connections=1,
                client_name=REDIS_CLIENT_NAME,
            ),
        )
        return cls(engine, redis, telemetry)

    async def is_ready(self) -> bool:
        if self._fresh():
            return self._ready
        check = self._check
        if check is None or check.done():
            # A clean context: the check outlives the probe that starts it, and its log
            # lines belong to no request.
            check = self._check = asyncio.create_task(
                self._run_checks(), context=contextvars.Context()
            )
        done, _ = await asyncio.wait({check}, timeout=READINESS_TIMEOUT_SECONDS)
        return check in done and check.result()

    async def close(self) -> None:
        """Stop running checks and close the connections (at shutdown), whatever fails."""
        try:
            for task in (self._check, self._redis_check):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
        finally:
            try:
                await self._engine.dispose()
            finally:
                await self._redis.aclose()

    def _fresh(self) -> bool:
        return (
            self._checked_at is not None
            and time.monotonic() - self._checked_at < READINESS_CACHE_SECONDS
        )

    async def _run_checks(self) -> bool:
        if self._redis_check is None or self._redis_check.done():
            self._redis_check = asyncio.create_task(
                self._check_redis(), context=contextvars.Context()
            )
        postgres = await self._check_postgres()
        self._ready, self._checked_at = postgres, time.monotonic()
        return postgres

    async def _check_postgres(self) -> bool:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                async with self._engine.connect() as connection:
                    await connection.execute(select(literal(1)))
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; it is logged
            self._failed("postgres", exc)
            return False
        self._succeeded("postgres")
        return True

    async def _check_redis(self) -> bool:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                await self._redis.ping()
        except Exception as exc:  # noqa: BLE001 - reported, never decisive
            self._measure_ping("error", started)
            self._failed("redis", exc)
            return False
        self._measure_ping("ok", started)
        self._succeeded("redis")
        return True

    def _measure_ping(self, outcome: str, started: float) -> None:
        with contextlib.suppress(Exception):  # reporting never changes the answer
            if self._telemetry is not None:
                self._telemetry.record_redis_operation(
                    "ping", outcome, time.perf_counter() - started
                )

    def _failed(self, dependency: str, error: Exception) -> None:
        # Reporting must never change the answer (a broken exporter is not an outage).
        with contextlib.suppress(Exception):
            if self._telemetry is not None:
                self._telemetry.record_dependency_unavailable(dependency, error)
        now = time.monotonic()
        logged_at = self._outage_logged_at.get(dependency)
        if logged_at is None or now - logged_at >= OUTAGE_REMINDER_SECONDS:
            self._outage_logged_at[dependency] = now
            with contextlib.suppress(Exception):
                logger.error("dependency_unavailable", dependency=dependency, exc_info=error)

    def _succeeded(self, dependency: str) -> None:
        if self._outage_logged_at.pop(dependency, None) is not None:
            with contextlib.suppress(Exception):
                logger.info("dependency_recovered", dependency=dependency)
