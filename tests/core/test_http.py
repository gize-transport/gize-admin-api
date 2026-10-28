"""GZ-008: the outbound HTTP client: only allowed public hosts, bounded, no leaks.

Outside services are simulated with httpx's MockTransport and a fixed resolver; the
client's own checks (allowlist, address, scheme, size, time, retries) are what is tested.
"""

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.http import (
    MAX_RESPONSE_BYTES,
    BlockedDestinationError,
    OpenTransactionError,
    OutboundClient,
    OutboundTimeoutError,
    OutboundUnavailableError,
    ResponseTooLargeError,
)
from gize_api.core.telemetry import configure_telemetry
from tests.support import SettingsFactory

HOST = "sms.example.com"
PUBLIC_IP = "93.184.216.34"


def _resolver(*addresses: str) -> Callable[[str, int], "asyncio.Future[list[str]]"]:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve  # type: ignore[return-value]


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *addresses: str,
    **kwargs: object,
) -> OutboundClient:
    return OutboundClient(
        allowed_hosts={HOST},
        resolver=_resolver(*(addresses or (PUBLIC_IP,))),
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def test_a_call_to_an_allowed_public_host(make_settings: SettingsFactory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as client:
        response = await client.request("POST", f"https://{HOST}/v1/send", json={"to": "x"})
    assert response.status == 200
    assert response.json() == {"ok": True}
    (request,) = seen
    # Connected to the address that was checked, with the real name for TLS and Host.
    assert request.url.host == PUBLIC_IP
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert "traceparent" not in request.headers


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example.com/x",  # not on the allowlist
        f"http://{HOST}/x",  # not https
        f"https://user:pw@{HOST}/x",  # credentials in the URL
        "https://SMS.EXAMPLE.COM.evil.test/x",
        "ftp://sms.example.com/x",
    ],
)
async def test_destinations_off_the_allowlist_are_blocked(url: str) -> None:
    async with _client(_ok) as client:
        with pytest.raises(BlockedDestinationError):
            await client.request("GET", url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 - a destination under test, not a bind address
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "224.0.0.1",
    ],
)
async def test_private_and_special_addresses_are_blocked(address: str) -> None:
    async with _client(_ok, address) as client:
        with pytest.raises(BlockedDestinationError):
            await client.request("GET", f"https://{HOST}/x")


async def test_one_private_address_among_public_ones_blocks_the_call() -> None:
    async with _client(_ok, PUBLIC_IP, "10.0.0.5") as client:
        with pytest.raises(BlockedDestinationError):
            await client.request("GET", f"https://{HOST}/x")


async def test_ipv6_public_address_is_bracketed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with _client(handler, "2606:2800:220:1:248:1893:25c8:1946") as client:
        await client.request("GET", f"https://{HOST}:8443/x")
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert seen[0].url.port == 8443
    assert seen[0].headers["Host"] == f"{HOST}:8443"


async def test_redirects_are_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/"})

    async with _client(handler) as client:
        response = await client.request("GET", f"https://{HOST}/x")
    assert response.status == 302


async def test_responses_over_the_cap_are_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    async with _client(handler) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.request("GET", f"https://{HOST}/x")


async def test_the_whole_call_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.http.TOTAL_TIMEOUT_SECONDS", 0.2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)  # pragma: no cover - never reached

    async with OutboundClient(
        allowed_hosts={HOST}, resolver=_resolver(PUBLIC_IP), transport=httpx.MockTransport(slow)
    ) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(OutboundTimeoutError):
            await client.request("GET", f"https://{HOST}/x")
        assert loop.time() - started < 1


async def test_a_post_is_never_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async with _client(handler) as client:
        response = await client.request("POST", f"https://{HOST}/send", json={})
    assert response.status == 503
    assert len(calls) == 1


async def test_a_post_connection_failure_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    async with _client(handler) as client:
        with pytest.raises(OutboundUnavailableError):
            await client.request("POST", f"https://{HOST}/send", json={})
    assert len(calls) == 1


async def test_safe_methods_are_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.http.RETRY_BASE_DELAY_SECONDS", 0.01)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        response = await client.request("GET", f"https://{HOST}/status")
    assert response.status == 200
    assert len(calls) == 3


async def test_retries_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.http.RETRY_BASE_DELAY_SECONDS", 0.01)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadError("reset")

    async with _client(handler) as client:
        with pytest.raises(OutboundUnavailableError):
            await client.request("GET", f"https://{HOST}/status")
    assert len(calls) == 3


async def test_resolution_failure_is_unavailable() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    async with OutboundClient(
        allowed_hosts={HOST}, resolver=failing, transport=httpx.MockTransport(_ok)
    ) as client:
        with pytest.raises(OutboundUnavailableError):
            await client.request("GET", f"https://{HOST}/x")


async def test_no_call_while_a_database_transaction_is_open(db_session: AsyncSession) -> None:
    """The connection would sit idle in a transaction during the call (GZ-006 review)."""
    async with _client(_ok) as client:
        async with db_session.begin():
            await db_session.execute(select(literal(1)))
            with pytest.raises(OpenTransactionError):
                await client.request("GET", f"https://{HOST}/x")
        assert (await client.request("GET", f"https://{HOST}/x")).status == 200


async def test_spans_and_metrics_name_the_host_never_the_path(
    make_settings: SettingsFactory,
) -> None:
    spans, metrics = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), span_exporter=spans, metric_reader=metrics)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={})

    tracer = telemetry.tracer_provider.get_tracer("test") if telemetry else trace.get_tracer("t")
    async with _client(handler, telemetry=telemetry) as client:
        with tracer.start_as_current_span("request"):  # an active trace: still no headers
            await client.request("POST", f"https://{HOST}/v1/send?to=0911223344", json={})
    assert "traceparent" not in seen[0].headers
    (span,) = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
    attributes = dict(span.attributes or {})
    assert span.name == "POST"
    assert attributes["server.address"] == HOST
    assert attributes["http.response.status_code"] == 201
    assert "0911223344" not in json.dumps(attributes)
    assert "/v1/send" not in json.dumps(attributes)
    data = metrics.get_metrics_data()
    assert data is not None
    points = [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "http.client.request.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    ]
    assert points == [
        {"server.address": HOST, "http.request.method": "POST", "http.response.status_code": 201}
    ]


def test_address_rules_match_the_standard_library() -> None:
    from gize_api.core.http import is_public_address

    assert is_public_address(ipaddress.ip_address(PUBLIC_IP))
    assert not is_public_address(ipaddress.ip_address("::ffff:10.0.0.1"))


async def test_a_malformed_url_is_blocked() -> None:
    async with _client(_ok) as client:
        with pytest.raises(BlockedDestinationError):
            await client.request("GET", f"https://{HOST}:99999/x")


async def test_a_streamed_response_over_the_cap_is_cut_off() -> None:
    """Without a Content-Length the cap is enforced while reading."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(MAX_RESPONSE_BYTES // 65536 + 2):
            yield b"x" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    async with _client(handler) as client:
        with pytest.raises(ResponseTooLargeError):
            await client.request("GET", f"https://{HOST}/x")


async def test_a_read_timeout_is_unavailable_and_marks_the_span(
    make_settings: SettingsFactory,
) -> None:
    spans = InMemorySpanExporter()
    telemetry = configure_telemetry(make_settings(), span_exporter=spans)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async with _client(handler, telemetry=telemetry) as client:
        with pytest.raises(OutboundUnavailableError):
            await client.request("POST", f"https://{HOST}/send", json={})
    (span,) = spans.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description is None
    assert dict(span.attributes or {})["error.type"] == "OutboundUnavailableError"


async def test_the_default_resolver_returns_addresses() -> None:
    from gize_api.core.http import _resolve

    assert "127.0.0.1" in await _resolve("127.0.0.1", 443)
