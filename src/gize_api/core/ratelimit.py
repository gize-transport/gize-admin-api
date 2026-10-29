"""Rate limiting: named policies, counted in Redis without ever blocking the server.

Built on `limits`' async API (sliding window counters). slowapi was replaced at GZ-009: it
calls Redis synchronously, so a slow Redis would stall every request.

- **Policies** (engineering-standards.md, "Rate limiting and abuse") name their limits,
  what each counts by (`staff`: the staff member, or the IP before login; `ip`; `phone`)
  and what happens if Redis is unavailable: staff traffic fails **open** (allowed, logged
  once a minute, counted), public and SMS traffic fails **closed** (503 + Retry-After).
- **Use:** routes add `Depends(rate_limited(POLICY))` after their auth dependency (it
  counts by staff ID and IP); services check phone limits with
  `await limiter.check(POLICY, phone=...)`. Health endpoints have no limits.
- **Over the limit:** 429 RATE_LIMITED with Retry-After. Logged as a security event (the
  policy, never the key); Sentry hears of it at most once a minute per policy, so a flood
  cannot exhaust the quota.
- **Keys** are hashed before they reach Redis: no phone number or IP is stored in clear.
- **The client's IP** is the socket address unless CLIENT_IP_HEADER names the header a
  trusted proxy writes (CF-Connecting-IP behind Cloudflare).
- Every check is bounded by CHECK_TIMEOUT_SECONDS and measured (db.client.operation.duration,
  db.system.name=redis, outcome allowed/limited/error).
"""

import asyncio
import hashlib
import ipaddress
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from fastapi import Request
from limits import RateLimitItem, parse
from limits.aio.storage import RedisStorage
from limits.aio.strategies import SlidingWindowCounterRateLimiter
from limits.errors import StorageError

from gize_api.core.config import Settings
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.logging import current_staff_id, log_security_event
from gize_api.core.redis_options import connection_options
from gize_api.core.telemetry import Telemetry

CHECK_TIMEOUT_SECONDS = 1.0
REDIS_MAX_CONNECTIONS = 20
UNAVAILABLE_RETRY_AFTER_SECONDS = "5"
OUTAGE_LOG_INTERVAL_SECONDS = 60.0
SENTRY_INTERVAL_SECONDS = 60.0
KEY_PREFIX = "gize-ratelimit"

KeyKind = Literal["staff", "ip", "phone"]

logger = structlog.get_logger("gize_api.ratelimit")


@dataclass(frozen=True)
class Limit:
    rate: str  # e.g. "120/minute"
    by: KeyKind


@dataclass(frozen=True)
class Policy:
    name: str
    limits: tuple[Limit, ...]
    fail_open: bool

    def __post_init__(self) -> None:
        for limit in self.limits:
            try:
                parse(limit.rate)
            except ValueError:
                raise ValueError(f"invalid rate {limit.rate!r} in policy {self.name}") from None


STAFF = Policy("staff", (Limit("120/minute", "staff"),), fail_open=True)
PUBLIC_SIGNUP = Policy(
    "public_signup", (Limit("5/hour", "ip"), Limit("3/day", "phone")), fail_open=False
)
SMS = Policy("sms", (Limit("10/hour", "ip"), Limit("3/hour", "phone")), fail_open=False)


class RateLimiter:
    def __init__(self, storage: RedisStorage, telemetry: Telemetry | None) -> None:
        self._storage = storage
        self._limiter = SlidingWindowCounterRateLimiter(storage)
        self._telemetry = telemetry
        self._outage_logged_at: dict[str, float] = {}
        self._reported_at: dict[str, float] = {}

    @classmethod
    def for_settings(
        cls, settings: Settings, *, telemetry: Telemetry | None, key_prefix: str = KEY_PREFIX
    ) -> "RateLimiter":
        url = settings.redis_url.get_secret_value()
        storage = RedisStorage(
            f"async+{url}",
            implementation="redispy",
            key_prefix=key_prefix,
            wrap_exceptions=True,  # every Redis failure arrives as StorageError
            **connection_options(
                url,
                timeout=CHECK_TIMEOUT_SECONDS,
                max_connections=REDIS_MAX_CONNECTIONS,
                client_name="gize-ratelimit",
            ),
        )
        return cls(storage, telemetry)

    async def aclose(self) -> None:
        client = getattr(getattr(self._storage, "bridge", None), "storage", None)
        if client is not None:
            await client.aclose()

    async def check(
        self,
        policy: Policy,
        *,
        staff_id: str | None = None,
        ip: str | None = None,
        phone: str | None = None,
    ) -> None:
        """Count one request against each of the policy's limits whose key is given.

        Raises AppError(RATE_LIMITED) over a limit, or AppError(SERVICE_UNAVAILABLE) when
        Redis is unavailable and the policy fails closed.
        """
        keys: dict[KeyKind, str | None] = {
            "staff": f"staff:{staff_id}" if staff_id else (f"ip:{ip}" if ip else None),
            "ip": f"ip:{ip}" if ip else None,
            "phone": f"phone:{phone}" if phone else None,
        }
        for index, limit in enumerate(policy.limits):
            key = keys[limit.by]
            if key is None:
                continue
            item = parse(limit.rate)
            identifiers = (policy.name, str(index), _digest(key))
            started = time.perf_counter()
            try:
                async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                    allowed = await self._limiter.hit(item, *identifiers)
            except (StorageError, OSError, TimeoutError) as exc:
                self._measure("error", started)
                self._unavailable(policy, exc)
                if policy.fail_open:
                    return
                raise AppError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    headers={"Retry-After": UNAVAILABLE_RETRY_AFTER_SECONDS},
                ) from None
            self._measure("allowed" if allowed else "limited", started)
            if not allowed:
                retry_after = await self._retry_after(item, identifiers)
                log_security_event(
                    "rate_limit_exceeded",
                    sentry=self._due(self._reported_at, policy.name, SENTRY_INTERVAL_SECONDS),
                    policy=policy.name,
                    limit=limit.by,
                )
                raise AppError(ErrorCode.RATE_LIMITED, headers={"Retry-After": str(retry_after)})

    async def _retry_after(self, item: RateLimitItem, identifiers: tuple[str, ...]) -> int:
        """Seconds until the window lets the caller in again (the window length if unknown)."""
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                stats = await self._limiter.get_window_stats(item, *identifiers)
        except (StorageError, OSError, TimeoutError):
            return item.get_expiry()
        return max(1, math.ceil(stats.reset_time - time.time()))

    def _unavailable(self, policy: Policy, error: BaseException) -> None:
        if self._due(self._outage_logged_at, policy.name, OUTAGE_LOG_INTERVAL_SECONDS):
            logger.warning(
                "rate_limit_unavailable",
                policy=policy.name,
                fail_open=policy.fail_open,
                exc_info=error,
            )

    def _measure(self, outcome: str, started: float) -> None:
        if self._telemetry is not None:
            self._telemetry.record_redis_operation(
                "rate_limit", outcome, time.perf_counter() - started
            )

    @staticmethod
    def _due(last: dict[str, float], name: str, interval: float) -> bool:
        now = time.monotonic()
        if name in last and now - last[name] < interval:
            return False
        last[name] = now
        return True


def rate_limited(policy: Policy) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency counting the request by staff ID (set by auth) and client IP.

    Declare it after the route's auth dependency, so the staff ID is known.
    """

    async def dependency(request: Request) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        settings: Settings = request.app.state.settings
        await limiter.check(
            policy,
            staff_id=current_staff_id(),
            ip=client_ip(request, settings.client_ip_header),
        )

    return dependency


def client_ip(request: Request, header: str | None) -> str:
    """The client's IP: from `header` when a trusted proxy writes it, else the socket.

    A value that is not exactly one IP address (missing, a list, garbage) falls back to the
    socket address: a request that bypassed the proxy cannot choose its key.
    """
    if header is not None:
        value = request.headers.get(header, "").strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    return request.client.host if request.client is not None else "unknown"


def _digest(key: str) -> str:
    """Keys are stored hashed: Redis never holds a phone number or IP in clear."""
    return hashlib.sha256(f"{KEY_PREFIX}:{key}".encode()).hexdigest()[:32]
