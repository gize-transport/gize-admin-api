"""Readiness: can this instance serve requests right now?

Readiness depends on Postgres only. Redis is checked too, and a failure is logged at ERROR
and counted, but never decides the answer: every instance shares one Redis, so failing
readiness on it would take the whole API offline (engineering-standards.md, "Health").

Both checks run at once, each bounded by READINESS_TIMEOUT_SECONDS. The endpoint is public,
so one result serves every probe for READINESS_CACHE_SECONDS, and concurrent probes share a
single check: a flood of requests never becomes a flood of connections.
"""

import asyncio
import time

import structlog
from redis.asyncio import Redis
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncEngine

from gize_api.core.telemetry import Telemetry

READINESS_TIMEOUT_SECONDS = 1.0
READINESS_CACHE_SECONDS = 1.0

logger = structlog.get_logger("gize_api.health")


class Readiness:
    def __init__(self, engine: AsyncEngine, redis: Redis, telemetry: Telemetry | None) -> None:
        self._engine = engine
        self._redis = redis
        self._telemetry = telemetry
        self._lock = asyncio.Lock()
        self._ready = False
        self._checked_at: float | None = None

    async def is_ready(self) -> bool:
        if self._fresh():
            return self._ready
        async with self._lock:
            if not self._fresh():  # another probe may have checked while this one waited
                postgres, _ = await asyncio.gather(self._check_postgres(), self._check_redis())
                self._ready, self._checked_at = postgres, time.monotonic()
        return self._ready

    def _fresh(self) -> bool:
        return (
            self._checked_at is not None
            and time.monotonic() - self._checked_at < READINESS_CACHE_SECONDS
        )

    async def _check_postgres(self) -> bool:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                async with self._engine.connect() as connection:
                    await connection.execute(select(literal(1)))
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; it is logged
            self._report("postgres", exc)
            return False
        return True

    async def _check_redis(self) -> bool:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                await self._redis.ping()
        except Exception as exc:  # noqa: BLE001 - reported, never decisive
            self._report("redis", exc)
            return False
        return True

    def _report(self, dependency: str, error: Exception) -> None:
        logger.error("dependency_unavailable", dependency=dependency, exc_info=error)
        if self._telemetry is not None:
            self._telemetry.record_dependency_unavailable(dependency, error)
