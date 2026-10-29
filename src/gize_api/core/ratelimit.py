"""Rate limiting: named policies, counted in Redis without ever blocking the server.

Built on `limits`' async API (sliding window counters). slowapi was replaced at GZ-009: it
calls Redis synchronously, so a slow Redis would stall every request.

- **Policies** (engineering-standards.md, "Rate limiting and abuse") name their limits,
  what each counts by (`staff`: the staff member, or the IP before login; `ip`; `phone`)
  and what happens if Redis is unavailable: staff traffic fails **open** (allowed, logged
  once a minute per process, counted as outcome "error": alert on it), public and SMS
  traffic fails **closed** (503 + Retry-After).
- **Use:** routes add `Depends(rate_limited(POLICY, after=<auth dependency>))`. FastAPI
  runs decorator dependencies before parameter ones; `after=` makes auth run first, so
  limits count per staff member, not per shared office IP. Services check phone limits
  with `await limiter.check(POLICY, phone=<E.164 number>)`: callers normalise the number,
  or `0911…` and `+251911…` would be separate counters. Health endpoints have no limits.
- **Order:** limits are checked in order and each check counts, so a request rejected by a
  later limit still uses up the earlier ones (the safe direction).
- **Over the limit:** 429 RATE_LIMITED with Retry-After from the window. Logged as a
  security event (the policy, never the key); Sentry hears of it at most once a minute
  per policy and process, so a flood cannot exhaust the quota.
- **Keys** reach Redis hashed: no phone number or IP is stored in clear. The hash is not
  secret (the number space is small), so this protects against casual reading only.
- **The client's IP** is the socket address unless CLIENT_IP_HEADER names the header a
  trusted proxy writes (CF-Connecting-IP behind Cloudflare). Then the origin must accept
  connections from that proxy only (firewall or authenticated origin pulls): a client
  reaching the origin directly could otherwise send any value. IPv6 clients are keyed by
  their /64 (one subscriber's allocation), IPv4-mapped forms as IPv4.
- **Redis:** the limiter has its own connection pool, which waits for a free connection
  rather than failing (a busy pool is not an outage). One deadline, CHECK_TIMEOUT_SECONDS,
  covers a whole check. Calls are traced (keys hashed) and every check is measured
  (db.client.operation.duration, db.system.name=redis, outcome allowed/limited/error).
"""

import asyncio
import hashlib
import ipaddress
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from fastapi import Depends, Request
from limits import RateLimitItem, parse
from limits.aio.storage import RedisStorage
from limits.aio.strategies import SlidingWindowCounterRateLimiter
from limits.errors import StorageError
from redis.asyncio import BlockingConnectionPool, Redis

from gize_api.core.config import Settings
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.logging import current_staff_id, log_security_event
from gize_api.core.redis_options import connection_options
from gize_api.core.telemetry import Telemetry

CHECK_TIMEOUT_SECONDS = 1.0
REDIS_MAX_CONNECTIONS = 20
UNAVAILABLE_RETRY_AFTER_SECONDS = "5"
FALLBACK_RETRY_AFTER_SECONDS = 60
OUTAGE_LOG_INTERVAL_SECONDS = 60.0
SENTRY_INTERVAL_SECONDS = 60.0
KEY_PREFIX = "gize-ratelimit"
IPV6_PREFIX_LENGTH = 64

KeyKind = Literal["staff", "ip", "phone"]

logger = structlog.get_logger("gize_api.ratelimit")
_unknown_client_logged_at: dict[str, float] = {}


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


@dataclass(frozen=True)
class _Exceeded:
    limit: Limit
    item: RateLimitItem
    identifiers: tuple[str, ...]


class RateLimiter:
    def __init__(
        self,
        storage: RedisStorage,
        pool: BlockingConnectionPool,
        telemetry: Telemetry | None,
    ) -> None:
        self._storage = storage
        self._pool = pool
        self._limiter = SlidingWindowCounterRateLimiter(storage)
        self._telemetry = telemetry
        self._outage_logged_at: dict[str, float] = {}
        self._reported_at: dict[str, float] = {}
        if telemetry is not None and self.redis is not None:
            telemetry.instrument_redis(self.redis)

    @classmethod
    def for_settings(
        cls, settings: Settings, *, telemetry: Telemetry | None, key_prefix: str = KEY_PREFIX
    ) -> "RateLimiter":
        url = settings.redis_url.get_secret_value()
        options = connection_options(
            url,
            timeout=CHECK_TIMEOUT_SECONDS,
            max_connections=REDIS_MAX_CONNECTIONS,
            client_name="gize-ratelimit",
        )
        # A blocking pool: a request waits (within the check's deadline) for a connection
        # instead of failing with "Too many connections" when the pool is busy.
        pool = BlockingConnectionPool.from_url(url, timeout=CHECK_TIMEOUT_SECONDS, **options)
        storage = RedisStorage(
            f"async+{url}",
            implementation="redispy",
            key_prefix=key_prefix,
            wrap_exceptions=True,  # every Redis failure arrives as StorageError
            connection_pool=pool,  # type: ignore[arg-type]  # accepted (RedispyBridge.use_basic); stubs say scalars only
        )
        return cls(storage, pool, telemetry)

    @property
    def redis(self) -> Redis | None:
        """The client `limits` built on our pool (for tracing)."""
        client = getattr(getattr(self._storage, "bridge", None), "storage", None)
        return client if isinstance(client, Redis) else None

    async def aclose(self) -> None:
        if self._telemetry is not None and self.redis is not None:
            self._telemetry.uninstrument_redis(self.redis)
        await self._pool.disconnect()

    async def check(
        self,
        policy: Policy,
        *,
        staff_id: str | None = None,
        ip: str | None = None,
        phone: str | None = None,
    ) -> None:
        """Count one request against each of the policy's limits whose key is given.

        `phone` must be a normalised (E.164) number. Raises AppError(RATE_LIMITED) over a
        limit, or AppError(SERVICE_UNAVAILABLE) when Redis is unavailable and the policy
        fails closed.
        """
        keys: dict[KeyKind, str | None] = {
            "staff": f"staff:{staff_id}" if staff_id else (f"ip:{ip}" if ip else None),
            "ip": f"ip:{ip}" if ip else None,
            "phone": f"phone:{phone}" if phone else None,
        }
        started = time.perf_counter()
        deadline = asyncio.get_running_loop().time() + CHECK_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                exceeded = await self._count(policy, keys)
        except (StorageError, OSError, TimeoutError) as exc:
            self._measure("error", started)
            self._unavailable(policy, exc)
            if policy.fail_open:
                return
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE,
                headers={"Retry-After": UNAVAILABLE_RETRY_AFTER_SECONDS},
            ) from None
        self._measure("allowed" if exceeded is None else "limited", started)
        if exceeded is None:
            return
        retry_after = await self._retry_after(exceeded, deadline)
        log_security_event(
            "rate_limit_exceeded",
            sentry=self._due(self._reported_at, policy.name, SENTRY_INTERVAL_SECONDS),
            policy=policy.name,
            limit=exceeded.limit.by,
        )
        raise AppError(ErrorCode.RATE_LIMITED, headers={"Retry-After": str(retry_after)})

    async def _count(self, policy: Policy, keys: dict[KeyKind, str | None]) -> _Exceeded | None:
        for index, limit in enumerate(policy.limits):
            key = keys[limit.by]
            if key is None:
                continue
            item = parse(limit.rate)
            identifiers = (policy.name, str(index), _digest(key))
            if not await self._limiter.hit(item, *identifiers):
                return _Exceeded(limit, item, identifiers)
        return None

    async def _retry_after(self, exceeded: _Exceeded, deadline: float) -> int:
        """Seconds until the window lets the caller in again (the window length if unknown)."""
        try:
            async with asyncio.timeout_at(deadline):
                stats = await self._limiter.get_window_stats(exceeded.item, *exceeded.identifiers)
        except (StorageError, OSError, TimeoutError):
            # Unknown: a short wait, never "come back tomorrow" because Redis was slow.
            return min(exceeded.item.get_expiry(), FALLBACK_RETRY_AFTER_SECONDS)
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


def rate_limited(
    policy: Policy, *, after: Callable[..., Any] | None = None
) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency counting the request by staff ID and client IP.

    Pass the route's auth dependency as `after`: FastAPI then resolves it first (and only
    once per request), so the staff ID is known when the request is counted. A policy
    with a `staff` limit requires it: without auth every staff member would be counted
    by the office's shared IP.
    """
    if after is None and any(limit.by == "staff" for limit in policy.limits):
        raise ValueError(f"policy {policy.name} counts by staff: pass after=<auth dependency>")

    async def dependency(request: Request, _: Any = Depends(after or _nothing)) -> None:  # noqa: ANN401, B008
        limiter: RateLimiter = request.app.state.rate_limiter
        settings: Settings = request.app.state.settings
        await limiter.check(
            policy,
            staff_id=current_staff_id(),
            ip=client_ip(request, settings.client_ip_header),
        )

    return dependency


async def _nothing() -> None:
    return None


def client_ip(request: Request, header: str | None) -> str:
    """The client's rate-limit address: from `header` when a trusted proxy writes it, else
    the socket; normalised (see the module docstring).

    A header value that is not exactly one IP address (missing, a list, garbage) falls back
    to the socket address. The header is only as trustworthy as the proxy in front: see
    CLIENT_IP_HEADER in the README.
    """
    if header is not None:
        address = _normalised(request.headers.get(header, ""))
        if address is not None:
            return address
    host = request.client.host if request.client is not None else ""
    address = _normalised(host)
    if address is None:
        if RateLimiter._due(_unknown_client_logged_at, "client", OUTAGE_LOG_INTERVAL_SECONDS):
            logger.warning("client_address_unknown")  # every such request shares one key
        return "unknown"
    return address


def _normalised(value: str) -> str | None:
    """One key per client: IPv4 as is (also when IPv4-mapped), IPv6 by its /64."""
    try:
        address = ipaddress.ip_address(value.strip().split("%", 1)[0])  # no scope ID
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, IPV6_PREFIX_LENGTH), strict=False))
    return str(address)


def _digest(key: str) -> str:
    """Keys reach Redis hashed (not secret: it stops casual reading, not an attacker)."""
    return hashlib.sha256(f"{KEY_PREFIX}:{key}".encode()).hexdigest()[:32]
