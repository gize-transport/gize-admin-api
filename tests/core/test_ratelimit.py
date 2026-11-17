"""GZ-009: rate limiting: named policies, per-caller keys, 429 + Retry-After, and the
fail-open / fail-closed rules when Redis is unavailable (engineering-standards.md).

Against real Redis; each test uses a key prefix of its own.
"""

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import Depends, Request
from limits.errors import StorageError
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from redis.asyncio import Redis

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.logging import set_staff_id
from gize_api.core.ratelimit import (
    PUBLIC_SIGNUP,
    SMS,
    STAFF,
    Limit,
    Policy,
    RateLimiter,
    client_ip,
    rate_limited,
)
from gize_api.core.telemetry import configure_telemetry
from gize_api.main import GizeAPI, create_app
from tests.support import SettingsFactory

TWO_PER_MINUTE = Policy("test_two", (Limit("2/minute", "staff"),), fail_open=True)
CLOSED = Policy("test_closed", (Limit("2/minute", "ip"),), fail_open=False)
PHONE = Policy(
    "test_phone", (Limit("10/minute", "ip"), Limit("2/minute", "phone")), fail_open=False
)


@pytest.fixture
async def prefix(test_redis_url: str) -> AsyncIterator[str]:
    name = f"gize-test-ratelimit-{uuid.uuid4()}"
    yield name
    redis = Redis.from_url(test_redis_url)
    try:
        keys = [key async for key in redis.scan_iter(f"{name}*")]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


def _app(make_settings: SettingsFactory, **overrides: Any) -> GizeAPI:  # noqa: ANN401
    app = create_app(make_settings(**overrides))

    async def as_staff(request: Request) -> None:  # stands in for GZ-010's auth dependency
        if staff := request.headers.get("x-test-staff"):
            set_staff_id(staff)

    @app.get("/_test/staff", dependencies=[Depends(rate_limited(TWO_PER_MINUTE, after=as_staff))])
    async def staff() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/_test/closed", dependencies=[Depends(rate_limited(CLOSED))])
    async def closed() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/_test/sms", dependencies=[Depends(rate_limited(PHONE))])
    async def sms(request: Request) -> dict[str, bool]:
        phone = (await request.json())["phone"]
        await request.app.state.rate_limiter.check(PHONE, phone=phone)  # a service's check
        return {"ok": True}

    return app


async def _client_for(
    app: GizeAPI, limiter: RateLimiter, ip: str = "203.0.113.7"
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 50000))
    async with app.router.lifespan_context(app):
        await app.state.rate_limiter.aclose()
        app.state.rate_limiter = limiter  # the app's own, with this test's key prefix
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def limiter_for(
    make_settings: SettingsFactory, test_redis_url: str, prefix: str
) -> Callable[..., RateLimiter]:
    def build(redis_url: str = test_redis_url, **settings: Any) -> RateLimiter:  # noqa: ANN401
        return RateLimiter.for_settings(
            make_settings(redis_url=redis_url, **settings), telemetry=None, key_prefix=prefix
        )

    return build


def _log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    return [line for line in lines if isinstance(line, dict)]


async def test_the_limit_is_enforced_with_429_and_retry_after(
    make_settings: SettingsFactory,
    limiter_for: Callable[..., RateLimiter],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for()):
        statuses = [(await client.get("/_test/staff")).status_code for _ in range(2)]
        limited = await client.get("/_test/staff")
    assert statuses == [200, 200]
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    (event,) = [line for line in _log_lines(capsys) if line["event"] == "rate_limit_exceeded"]
    assert event["policy"] == "test_two"
    assert event["level"] == "warning"
    assert "203.0.113.7" not in json.dumps(event)  # never the key


async def test_staff_are_counted_separately_and_by_id_not_ip(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for()):
        alice = [
            (await client.get("/_test/staff", headers={"x-test-staff": "alice"})).status_code
            for _ in range(3)
        ]
        bob = (await client.get("/_test/staff", headers={"x-test-staff": "bob"})).status_code
        # Without a staff ID, by IP: a bucket of its own, exhausted separately.
        anonymous = [(await client.get("/_test/staff")).status_code for _ in range(3)]
    assert alice == [200, 200, 429]
    assert bob == 200
    assert anonymous == [200, 200, 429]


async def test_clients_are_counted_by_socket_address_by_default(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    """Without CLIENT_IP_HEADER, a forged CF-Connecting-IP changes nothing."""
    async for client in _client_for(_app(make_settings), limiter_for()):
        statuses = [
            (
                await client.get("/_test/closed", headers={"CF-Connecting-IP": f"198.51.100.{n}"})
            ).status_code
            for n in range(3)
        ]
    assert statuses == [200, 200, 429]


async def test_the_configured_header_names_the_client(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    app = _app(make_settings, client_ip_header="CF-Connecting-IP")
    async for client in _client_for(app, limiter_for()):
        one = [
            (
                await client.get("/_test/closed", headers={"CF-Connecting-IP": "198.51.100.1"})
            ).status_code
            for _ in range(3)
        ]
        other = (
            await client.get("/_test/closed", headers={"CF-Connecting-IP": "198.51.100.2"})
        ).status_code
    assert one == [200, 200, 429]
    assert other == 200


@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("198.51.100.9", "198.51.100.9"),
        (" 2001:db8::5 ", "2001:db8::/64"),  # a client holds a whole /64
        ("2001:db8::6", "2001:db8::/64"),  # same network, same key
        ("::ffff:198.51.100.9", "198.51.100.9"),  # one address, one key
        ("fe80::1%eth0", "fe80::/64"),
        ("not an ip", "203.0.113.7"),  # unusable: the socket address
        ("198.51.100.9, 10.0.0.1", "203.0.113.7"),  # a list is not what the proxy writes
        (None, "203.0.113.7"),  # a request that bypassed the proxy
    ],
)
def test_client_ip(header_value: str | None, expected: str) -> None:
    headers = [] if header_value is None else [(b"cf-connecting-ip", header_value.encode())]
    request = Request({"type": "http", "headers": headers, "client": ("203.0.113.7", 1)})
    assert client_ip(request, "cf-connecting-ip") == expected
    assert client_ip(request, None) == "203.0.113.7"
    assert client_ip(Request({"type": "http", "headers": []}), None) == "unknown"


async def test_services_check_phone_limits(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for()):
        same = [
            (await client.post("/_test/sms", json={"phone": "+251911223344"})).status_code
            for _ in range(3)
        ]
        other = (await client.post("/_test/sms", json={"phone": "+251911000000"})).status_code
    assert same == [200, 200, 429]
    assert other == 200


async def test_redis_holds_no_phone_number_or_ip(
    make_settings: SettingsFactory,
    limiter_for: Callable[..., RateLimiter],
    test_redis_url: str,
    prefix: str,
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for()):
        await client.post("/_test/sms", json={"phone": "+251911223344"})
    redis = Redis.from_url(test_redis_url)
    try:
        keys = [key async for key in redis.scan_iter(f"{prefix}*")]
    finally:
        await redis.aclose()
    assert keys
    for key in keys:
        assert b"911223344" not in key
        assert b"203.0.113.7" not in key


async def test_concurrent_requests_are_counted_exactly(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    policy = Policy("test_ten", (Limit("10/minute", "ip"),), fail_open=False)
    limiter = limiter_for()
    try:
        outcomes = await asyncio.gather(
            *(_allowed(limiter, policy) for _ in range(25)), return_exceptions=False
        )
    finally:
        await limiter.aclose()
    assert outcomes.count(True) == 10


async def _allowed(limiter: RateLimiter, policy: Policy, ip: str = "198.51.100.1") -> bool:
    """True if allowed, False if limited; any other outcome (503) fails the test."""
    try:
        await limiter.check(policy, ip=ip)
    except AppError as exc:
        if exc.code is not ErrorCode.RATE_LIMITED:
            raise
        return False
    return True


async def test_redis_down_lets_staff_through_and_says_so_once(
    make_settings: SettingsFactory,
    limiter_for: Callable[..., RateLimiter],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for("redis://127.0.0.1:1/0")):
        statuses = [(await client.get("/_test/staff")).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]
    warnings = [line for line in _log_lines(capsys) if line["event"] == "rate_limit_unavailable"]
    assert len(warnings) == 1  # once a minute, not once per request
    assert warnings[0]["level"] == "warning"
    assert warnings[0]["policy"] == "test_two"


@pytest.mark.parametrize("path", ["/_test/closed", "/_test/sms"])
async def test_redis_down_refuses_public_and_sms_endpoints(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter], path: str
) -> None:
    async for client in _client_for(_app(make_settings), limiter_for("redis://127.0.0.1:1/0")):
        if path == "/_test/sms":
            response = await client.post(path, json={"phone": "+251911223344"})
        else:
            response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["Retry-After"]


async def test_a_hung_redis_costs_at_most_the_timeout(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter], blackhole_url: str
) -> None:
    hung = "redis://" + blackhole_url.removeprefix("http://") + "/0"
    async for client in _client_for(_app(make_settings), limiter_for(hung)):
        started = time.monotonic()
        response = await client.get("/_test/staff")
        elapsed = time.monotonic() - started
    assert response.status_code == 200  # staff: fail open
    assert elapsed < 2.5


async def test_sentry_hears_of_a_flood_once_a_minute_per_policy(
    make_settings: SettingsFactory,
    limiter_for: Callable[..., RateLimiter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "gize_api.core.logging.sentry_sdk.capture_message", lambda event, **_: sent.append(event)
    )
    async for client in _client_for(_app(make_settings), limiter_for()):
        for _ in range(10):
            await client.get("/_test/staff")
    assert sent == ["rate_limit_exceeded"]


async def test_checks_are_measured(
    make_settings: SettingsFactory, test_redis_url: str, prefix: str
) -> None:
    metrics = InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    policy = Policy("test_one", (Limit("1/minute", "ip"),), fail_open=True)
    limiter = RateLimiter.for_settings(
        make_settings(redis_url=test_redis_url), telemetry=telemetry, key_prefix=prefix
    )
    down = RateLimiter.for_settings(
        make_settings(redis_url="redis://127.0.0.1:1/0"), telemetry=telemetry, key_prefix=prefix
    )
    try:
        await limiter.check(policy, ip="198.51.100.1")
        with pytest.raises(AppError):
            await limiter.check(policy, ip="198.51.100.1")
        await down.check(policy, ip="198.51.100.1")
    finally:
        await limiter.aclose()
        await down.aclose()
    data = metrics.get_metrics_data()
    assert data is not None
    outcomes = sorted(
        str((point.attributes or {})["outcome"])
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "db.client.operation.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
        and (point.attributes or {}).get("db.operation.name") == "rate_limit"
    )
    assert outcomes == ["allowed", "error", "limited"]


def test_the_standard_policies() -> None:
    """engineering-standards.md, "Rate limiting and abuse"."""
    assert STAFF.limits == (Limit("120/minute", "staff"),)
    assert STAFF.fail_open
    assert PUBLIC_SIGNUP.limits == (Limit("5/hour", "ip"), Limit("3/day", "phone"))
    assert not PUBLIC_SIGNUP.fail_open
    assert SMS.limits == (Limit("10/hour", "ip"), Limit("3/hour", "phone"))
    assert not SMS.fail_open


def test_a_policy_needs_a_valid_rate() -> None:
    with pytest.raises(ValueError, match="rate"):
        Policy("bad", (Limit("lots", "ip"),), fail_open=True)


async def test_retry_after_falls_back_to_the_window_length(
    limiter_for: Callable[..., RateLimiter],
) -> None:
    """If Redis fails right after the hit, the caller is told the whole window."""
    from limits import parse

    from gize_api.core.ratelimit import _Exceeded

    down = limiter_for("redis://127.0.0.1:1/0")
    exceeded = _Exceeded(Limit("3/minute", "ip"), parse("3/minute"), ("p", "0", "k"))
    deadline = asyncio.get_running_loop().time() + 1
    try:
        assert await down._retry_after(exceeded, deadline) == 60
        daily = _Exceeded(Limit("3/day", "phone"), parse("3/day"), ("p", "1", "k"))
        # A slow Redis must not tell a caller to come back tomorrow: capped at a minute.
        assert await down._retry_after(daily, deadline) == 60
    finally:
        await down.aclose()


async def test_closing_tolerates_a_storage_without_a_client(
    limiter_for: Callable[..., RateLimiter],
) -> None:
    from types import SimpleNamespace

    limiter = limiter_for()
    await limiter.aclose()
    limiter._storage = SimpleNamespace()  # type: ignore[assignment]
    await limiter.aclose()  # nothing to close, no error


async def test_more_concurrent_checks_than_connections_wait_instead_of_failing(
    limiter_for: Callable[..., RateLimiter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full pool is a queue, never "Redis down" (GZ-009 review: 22 of 100 got 503)."""
    monkeypatch.setattr("gize_api.core.ratelimit.REDIS_MAX_CONNECTIONS", 2)
    wide = Policy("test_wide", (Limit("1000/minute", "ip"),), fail_open=False)
    narrow = Policy("test_narrow", (Limit("5/minute", "ip"),), fail_open=True)
    limiter = limiter_for()
    try:
        wide_outcomes = await asyncio.gather(*(_allowed(limiter, wide) for _ in range(40)))
        narrow_outcomes = await asyncio.gather(*(_allowed(limiter, narrow) for _ in range(40)))
    finally:
        await limiter.aclose()
    assert all(wide_outcomes)  # no 503: _allowed raises on anything but RATE_LIMITED
    assert narrow_outcomes.count(True) == 5  # no fail-open bypass under load


async def test_auth_declared_as_a_parameter_still_runs_first(
    make_settings: SettingsFactory, limiter_for: Callable[..., RateLimiter]
) -> None:
    """FastAPI runs decorator dependencies before parameter ones; `after=` fixes the order."""
    app = create_app(make_settings())

    async def current_staff(request: Request) -> str | None:  # GZ-010's auth, in spirit
        staff = request.headers.get("x-test-staff")
        set_staff_id(staff)
        return staff

    @app.get(
        "/_test/auth", dependencies=[Depends(rate_limited(TWO_PER_MINUTE, after=current_staff))]
    )
    async def handler(staff: str | None = Depends(current_staff)) -> dict[str, str | None]:
        return {"staff": staff}

    async for client in _client_for(app, limiter_for()):
        alice = [
            (await client.get("/_test/auth", headers={"x-test-staff": "alice"})).status_code
            for _ in range(3)
        ]
        bob = (await client.get("/_test/auth", headers={"x-test-staff": "bob"})).status_code
    assert alice == [200, 200, 429]
    assert bob == 200  # counted by staff ID, not the shared IP


async def test_a_rejected_request_still_counts_against_the_limits_before(
    limiter_for: Callable[..., RateLimiter],
) -> None:
    """Pinned: limits are checked in order and each check counts (the safe direction)."""
    policy = Policy(
        "test_order", (Limit("2/minute", "ip"), Limit("1/minute", "phone")), fail_open=False
    )
    limiter = limiter_for()
    try:
        await limiter.check(policy, ip="198.51.100.1", phone="+251911000001")
        with pytest.raises(AppError):  # the phone limit rejects; the IP limit counted it
            await limiter.check(policy, ip="198.51.100.1", phone="+251911000001")
        with pytest.raises(AppError):  # so the IP limit is now spent, for any phone
            await limiter.check(policy, ip="198.51.100.1", phone="+251911000002")
    finally:
        await limiter.aclose()


async def test_one_deadline_covers_the_whole_check(
    make_settings: SettingsFactory,
    limiter_for: Callable[..., RateLimiter],
    blackhole_url: str,
) -> None:
    two_limits = Policy(
        "test_two_limits", (Limit("5/minute", "ip"), Limit("5/minute", "phone")), fail_open=True
    )
    limiter = limiter_for("redis://" + blackhole_url.removeprefix("http://") + "/0")
    started = time.monotonic()
    try:
        await limiter.check(two_limits, ip="198.51.100.1", phone="+251911000001")
    finally:
        await limiter.aclose()
    assert time.monotonic() - started < 1.5


async def test_the_limiters_redis_calls_are_traced_without_keys_in_clear(
    make_settings: SettingsFactory, test_redis_url: str, prefix: str
) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    spans = InMemorySpanExporter()
    telemetry = configure_telemetry(make_settings(), span_exporter=spans)
    limiter = RateLimiter.for_settings(
        make_settings(redis_url=test_redis_url), telemetry=telemetry, key_prefix=prefix
    )
    try:
        await limiter.check(PHONE, ip="198.51.100.1", phone="+251911223344")
    finally:
        await limiter.aclose()
    redis_spans = [
        s for s in spans.get_finished_spans() if (s.attributes or {}).get("db.system") == "redis"
    ]
    assert redis_spans
    assert "911223344" not in str([dict(s.attributes or {}) for s in redis_spans])


def test_an_unknown_client_address_is_logged(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every such request shares one bucket: a misconfigured server, worth saying."""
    from gize_api.core.logging import configure_logging

    monkeypatch.setattr("gize_api.core.ratelimit._unknown_client_logged_at", {})
    configure_logging("INFO")
    assert client_ip(Request({"type": "http", "headers": []}), None) == "unknown"
    assert any("client_address_unknown" in line for line in capsys.readouterr().err.splitlines())


@pytest.mark.parametrize("header_value", [None, "not an ip", "198.51.100.9, 10.0.0.1"])
def test_a_missing_trusted_header_is_logged_once_a_minute(
    header_value: str | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GZ-072: behind Render, the socket address is the proxy's, shared by every caller. If
    the header goes missing (a host change, or a server calling for its visitors), say so:
    the name of the header only, never what was sent."""
    from gize_api.core.logging import configure_logging

    monkeypatch.setattr("gize_api.core.ratelimit._unknown_client_logged_at", {})
    configure_logging("INFO")
    headers = [] if header_value is None else [(b"cf-connecting-ip", header_value.encode())]
    request = Request({"type": "http", "headers": headers, "client": ("203.0.113.7", 1)})
    assert client_ip(request, "cf-connecting-ip") == "203.0.113.7"
    assert client_ip(request, "cf-connecting-ip") == "203.0.113.7"
    lines = [line for line in capsys.readouterr().err.splitlines() if "client_ip_header" in line]
    assert len(lines) == 1  # not once per request
    event = json.loads(lines[0])
    assert event["event"] == "client_ip_header_missing"
    assert event["header"] == "cf-connecting-ip"
    assert event["level"] == "warning"
    assert "198.51.100" not in lines[0]
    assert "not an ip" not in lines[0]


def test_a_present_trusted_header_logs_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gize_api.core.logging import configure_logging

    monkeypatch.setattr("gize_api.core.ratelimit._unknown_client_logged_at", {})
    configure_logging("INFO")
    headers = [(b"cf-connecting-ip", b"198.51.100.9")]
    request = Request({"type": "http", "headers": headers, "client": ("203.0.113.7", 1)})
    assert client_ip(request, "cf-connecting-ip") == "198.51.100.9"
    assert "client_ip_header" not in capsys.readouterr().err


async def test_a_failing_limiter_close_does_not_stop_shutdown(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failing(self: RateLimiter) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(RateLimiter, "aclose", failing)
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        pass
    assert '"rate_limiter_close_failed"' in capsys.readouterr().err


def test_a_staff_policy_requires_its_auth_dependency() -> None:
    """Forgetting `after=` would silently count every staff member by the office IP."""
    with pytest.raises(ValueError, match="after="):
        rate_limited(TWO_PER_MINUTE)
    rate_limited(CLOSED)  # IP and phone policies need no auth


# GZ-073: after a failure, skip Redis until a background check finds it again


class _SwitchableRedis:
    """A TCP proxy in front of the test Redis that can be taken down and brought back: down,
    it closes every connection (open ones too) as soon as it is made."""

    def __init__(self, target: str) -> None:
        url = urlsplit(target)
        self._target = (url.hostname or "127.0.0.1", url.port or 6379)
        self._db = url.path or "/0"
        self.up = True
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"redis://127.0.0.1:{port}{self._db}"

    def take_down(self) -> None:
        self.up = False
        for writer in list(self._writers):
            writer.close()

    async def stop(self) -> None:
        self.take_down()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.up:
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        self._writers |= {writer, upstream_writer}

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            with contextlib.suppress(OSError):
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            sink.close()

        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        self._writers -= {writer, upstream_writer}


@pytest.fixture
async def switchable_redis(test_redis_url: str) -> AsyncIterator[tuple[_SwitchableRedis, str]]:
    proxy = _SwitchableRedis(test_redis_url)
    url = await proxy.start()
    yield proxy, url
    await proxy.stop()


@pytest.fixture
def quick_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.ratelimit.PROBE_INTERVAL_SECONDS", 0.05)


async def test_after_a_hung_check_requests_skip_redis(
    limiter_for: Callable[..., RateLimiter], blackhole_url: str
) -> None:
    """The GZ-063 drill: with Redis gone, every request waited out the 1 s timeout."""
    limiter = limiter_for("redis://" + blackhole_url.removeprefix("http://") + "/0")
    try:
        started = time.monotonic()
        await limiter.check(STAFF, ip="198.51.100.1")  # waits for the timeout, once
        first = time.monotonic() - started
        started = time.monotonic()
        for _ in range(20):
            await limiter.check(STAFF, ip="198.51.100.1")
        rest = time.monotonic() - started
    finally:
        await limiter.aclose()
    assert first >= 0.9
    assert rest < 0.2  # no Redis call at all


async def test_while_skipping_a_closed_policy_refuses_at_once(
    limiter_for: Callable[..., RateLimiter], blackhole_url: str
) -> None:
    limiter = limiter_for("redis://" + blackhole_url.removeprefix("http://") + "/0")
    try:
        await limiter.check(STAFF, ip="198.51.100.1")  # the failure
        started = time.monotonic()
        with pytest.raises(AppError) as refused:
            await limiter.check(SMS, ip="198.51.100.1", phone="+251911223344")
        elapsed = time.monotonic() - started
    finally:
        await limiter.aclose()
    assert refused.value.code is ErrorCode.SERVICE_UNAVAILABLE
    assert refused.value.headers == {"Retry-After": "5"}
    assert elapsed < 0.1


@pytest.mark.usefixtures("quick_probe")
async def test_the_limits_return_when_redis_answers_again(
    limiter_for: Callable[..., RateLimiter],
    switchable_redis: tuple[_SwitchableRedis, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gize_api.core.logging import configure_logging

    configure_logging("INFO")
    proxy, url = switchable_redis
    one = Policy("test_one", (Limit("1/minute", "ip"),), fail_open=True)
    limiter = limiter_for(url)
    try:
        await limiter.check(one, ip="198.51.100.1")  # counted
        proxy.take_down()
        await limiter.check(one, ip="198.51.100.1")  # fails open: Redis is down
        await limiter.check(one, ip="198.51.100.1")  # skipped
        await asyncio.sleep(0.3)  # several background checks, all failing
        await limiter.check(one, ip="198.51.100.1")  # still skipped
        proxy.up = True
        await _until(lambda: not limiter.skipping_redis)
        with pytest.raises(AppError) as limited:
            await limiter.check(one, ip="198.51.100.1")  # counted again: over the limit
    finally:
        await limiter.aclose()
    assert limited.value.code is ErrorCode.RATE_LIMITED
    events = [line["event"] for line in _log_lines(capsys)]
    assert events.count("rate_limit_unavailable") == 1  # once a minute per policy
    assert events.count("rate_limit_recovered") == 1


async def test_skipped_checks_are_not_measured_and_said_once_a_minute_per_policy(
    make_settings: SettingsFactory,
    prefix: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gize_api.core.logging import configure_logging

    configure_logging("INFO")
    metrics = InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    down = RateLimiter.for_settings(
        make_settings(redis_url="redis://127.0.0.1:1/0"), telemetry=telemetry, key_prefix=prefix
    )
    try:
        await down.check(STAFF, ip="198.51.100.1")  # the failure
        for _ in range(3):
            await down.check(STAFF, ip="198.51.100.1")
            with pytest.raises(AppError):
                await down.check(SMS, ip="198.51.100.1")
    finally:
        await down.aclose()
    data = metrics.get_metrics_data()
    assert data is not None
    counts = {
        str((point.attributes or {})["outcome"]): point.count
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "db.client.operation.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
        and (point.attributes or {}).get("db.operation.name") == "rate_limit"
    }
    assert counts == {"error": 1}  # skipped checks made no Redis call: no 0 s timings
    lines = [line for line in _log_lines(capsys) if line["event"] == "rate_limit_unavailable"]
    assert [(line["policy"], line["skipped"]) for line in lines] == [
        ("staff", False),
        ("sms", True),
    ]


@pytest.mark.usefixtures("quick_probe")
async def test_the_background_check_is_measured(
    make_settings: SettingsFactory,
    prefix: str,
    switchable_redis: tuple[_SwitchableRedis, str],
) -> None:
    """While requests skip Redis, its errors keep coming from the background check."""
    proxy, url = switchable_redis
    metrics = InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    limiter = RateLimiter.for_settings(
        make_settings(redis_url=url), telemetry=telemetry, key_prefix=prefix
    )
    try:
        proxy.take_down()
        await limiter.check(STAFF, ip="198.51.100.1")
        await asyncio.sleep(0.2)
        proxy.up = True
        await _until(lambda: not limiter.skipping_redis)
    finally:
        await limiter.aclose()
    data = metrics.get_metrics_data()
    assert data is not None
    probes = {
        str((point.attributes or {})["outcome"])
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "db.client.operation.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
        and (point.attributes or {}).get("db.operation.name") == "rate_limit_probe"
    }
    assert probes == {"error", "ok"}


async def test_requests_failing_together_start_one_background_check(
    limiter_for: Callable[..., RateLimiter],
    blackhole_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = 0
    real = RateLimiter._until_redis_answers

    async def counting(self: RateLimiter) -> None:
        nonlocal started
        started += 1
        await real(self)

    monkeypatch.setattr(RateLimiter, "_until_redis_answers", counting)
    limiter = limiter_for("redis://" + blackhole_url.removeprefix("http://") + "/0")
    try:
        await asyncio.gather(*(limiter.check(STAFF, ip="198.51.100.1") for _ in range(5)))
        await asyncio.sleep(0)  # let the task start
    finally:
        await limiter.aclose()
    assert started == 1


async def test_closing_stops_the_background_check(
    limiter_for: Callable[..., RateLimiter],
) -> None:
    limiter = limiter_for("redis://127.0.0.1:1/0")
    try:
        await limiter.check(STAFF, ip="198.51.100.1")
        probe = limiter._probe
        assert probe is not None
        assert not probe.done()
    finally:
        await limiter.aclose()
    assert probe.cancelled()
    assert not limiter.skipping_redis
    await limiter.check(STAFF, ip="198.51.100.1")  # failing during shutdown
    assert limiter._probe is None  # starts nothing new


@pytest.mark.usefixtures("quick_probe")
async def test_a_background_check_that_hangs_is_cut_off(
    limiter_for: Callable[..., RateLimiter],
    blackhole_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PING that never answers costs one timeout and the next one runs. Closing stops the
    loop even mid-PING (`limits`' own check swallows the cancellation)."""
    limiter = limiter_for("redis://" + blackhole_url.removeprefix("http://") + "/0")
    pings = 0
    real_ping = limiter._ping_client.ping

    async def counting_ping() -> bool:
        nonlocal pings
        pings += 1
        return bool(await real_ping())

    monkeypatch.setattr(limiter._ping_client, "ping", counting_ping)
    monkeypatch.setattr("gize_api.core.ratelimit.CHECK_TIMEOUT_SECONDS", 0.2)
    await limiter.check(STAFF, ip="198.51.100.1")
    await _until(lambda: pings >= 3, within=3.0)
    await _until(lambda: pings == 4, within=1.0)  # now mid-PING, most likely
    async with asyncio.timeout(1.0):
        await limiter.aclose()
    assert not limiter.skipping_redis
    assert not limiter._pool._in_use_connections  # cut-off PINGs gave their connections back


async def _until(condition: Callable[[], bool], within: float = 2.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_one_failure_against_a_healthy_redis_skips_only_briefly(
    limiter_for: Callable[..., RateLimiter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-off failure (a stalled loop, a busy pool) must not turn the limits off for long:
    the background check PINGs at once."""
    limiter = limiter_for()
    real = limiter._count
    failures = iter([StorageError(ConnectionError("one-off"))])

    async def failing_once(*args: Any) -> Any:  # noqa: ANN401
        if (error := next(failures, None)) is not None:
            raise error
        return await real(*args)

    monkeypatch.setattr(limiter, "_count", failing_once)
    one = Policy("test_one", (Limit("1/minute", "ip"),), fail_open=False)
    try:
        with pytest.raises(AppError):  # closed: 503
            await limiter.check(one, ip="198.51.100.1")
        started = time.monotonic()
        await _until(lambda: not limiter.skipping_redis, within=0.5)
        skipped_for = time.monotonic() - started
        await limiter.check(one, ip="198.51.100.1")  # counted
        with pytest.raises(AppError) as limited:
            await limiter.check(one, ip="198.51.100.1")
    finally:
        await limiter.aclose()
    assert skipped_for < 0.5
    assert limited.value.code is ErrorCode.RATE_LIMITED


@pytest.mark.usefixtures("quick_probe")
async def test_an_unexpected_error_in_the_background_check_never_leaves_it_skipping(
    limiter_for: Callable[..., RateLimiter],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Skipping for good would leave staff limits off and sign-up refusing until a restart."""
    from gize_api.core.logging import configure_logging

    configure_logging("INFO")
    limiter = limiter_for()
    real_ping = limiter._ping_client.ping
    surprises = iter([RuntimeError("parser bug"), RuntimeError("parser bug")])

    async def surprising_ping() -> bool:
        if (error := next(surprises, None)) is not None:
            raise error
        return bool(await real_ping())

    monkeypatch.setattr(limiter._ping_client, "ping", surprising_ping)
    try:
        limiter._start_skipping()
        await _until(lambda: not limiter.skipping_redis)
    finally:
        await limiter.aclose()
    events = [line["event"] for line in _log_lines(capsys)]
    assert events.count("rate_limit_probe_failed") == 1  # once a minute
    assert events.count("rate_limit_recovered") == 1


async def test_a_background_check_that_dies_ends_the_skipping(
    limiter_for: Callable[..., RateLimiter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: even if the task itself fails, the next check tries Redis again."""
    limiter = limiter_for()

    async def broken(self: RateLimiter) -> bool:
        raise RuntimeError("broken")

    monkeypatch.setattr(RateLimiter, "_ping", broken)
    try:
        limiter._start_skipping()
        probe = limiter._probe
        assert probe is not None
        with pytest.raises(RuntimeError):
            await probe
        assert not limiter.skipping_redis
        await limiter.check(STAFF, ip="198.51.100.1")  # goes to Redis, which answers
    finally:
        await limiter.aclose()
