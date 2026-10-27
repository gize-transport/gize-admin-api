"""GZ-005: OpenTelemetry traces and metrics; off unless configured; never leaks request data."""

import asyncio
import subprocess
import sys
import time
from collections.abc import Sequence

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN
from redis.asyncio import Redis
from starlette.types import Message, Receive, Scope, Send

from gize_api.core.request_id import REQUEST_ID_HEADER
from gize_api.core.telemetry import (
    _DropInboundTraceContext,
    _resolve,
    _scrub_request_span,
    configure_telemetry,
)
from gize_api.main import GizeAPI, create_app
from tests.support import SettingsFactory

PHONE = "0911223344"
SENSITIVE = (PHONE, "phone=", "10.9.8.7", "evil-host.example", "Mozilla/5.0 (Secret Phone)")


def _client(app: FastAPI, host: str = "test") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("10.9.8.7", 5555))
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}")


def _app(make_settings: SettingsFactory, **overrides: object) -> GizeAPI:
    app = create_app(make_settings(**overrides))

    @app.get("/_test/riders/{rider_id}")
    async def rider(rider_id: str) -> dict[str, str]:
        return {"rider_id": rider_id}

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("boom 0911223344")

    return app


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def metrics() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def instrumented(
    make_settings: SettingsFactory, spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> GizeAPI:
    app = _app(make_settings)
    app.telemetry = configure_telemetry(make_settings(), span_exporter=spans, metric_reader=metrics)
    return app


def _attribute_values(finished: Sequence[ReadableSpan]) -> list[str]:
    return [str(v) for span in finished for v in (span.attributes or {}).values()]


def _server_spans(spans: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]


# --- configuration ----------------------------------------------------------------------


def test_disabled_without_an_endpoint(make_settings: SettingsFactory) -> None:
    assert configure_telemetry(make_settings()) is None


async def test_otlp_exporters_configured_from_settings(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        otel_exporter_otlp_endpoint="http://127.0.0.1:1",  # refused: nothing leaves the machine
        otel_exporter_otlp_headers="Authorization=Basic eA==",
        release="2026.10.27-abc123",
    )
    telemetry = configure_telemetry(settings)
    assert telemetry is not None
    resource = telemetry.tracer_provider.resource.attributes
    assert resource["service.name"] == "gize-admin-api"
    assert resource["service.version"] == "2026.10.27-abc123"
    assert resource["deployment.environment.name"] == "test"
    await telemetry.shutdown()


async def test_fastapi_builtin_telemetry_stays_off(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FastAPI 0.162 traces by default once a global provider exists; ours must not."""
    global_spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(global_spans))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    app = _app(make_settings)
    async with _client(app) as client:
        await client.get(f"/_test/riders/{PHONE}", params={"phone": PHONE})
    assert global_spans.get_finished_spans() == ()


# --- request spans ----------------------------------------------------------------------


async def test_request_span_named_by_route_template_without_request_data(
    instrumented: GizeAPI, spans: InMemorySpanExporter
) -> None:
    async with _client(instrumented, host="evil-host.example") as client:
        response = await client.get(
            f"/_test/riders/{PHONE}",
            params={"phone": PHONE},
            headers={"User-Agent": "Mozilla/5.0 (Secret Phone)"},
        )
    (server,) = _server_spans(spans)
    assert server.name == "GET /_test/riders/{rider_id}"
    attributes = dict(server.attributes or {})
    assert attributes["http.route"] == "/_test/riders/{rider_id}"
    assert attributes["gize.request_id"] == response.headers[REQUEST_ID_HEADER]
    for value in _attribute_values(spans.get_finished_spans()):
        for sensitive in SENSITIVE:
            assert sensitive not in value


async def test_500_is_an_error_span_without_exception_text(
    instrumented: GizeAPI, spans: InMemorySpanExporter
) -> None:
    async with _client(instrumented) as client:
        await client.get("/_test/crash")
    (server,) = _server_spans(spans)
    attributes = server.attributes or {}
    status = attributes.get("http.response.status_code", attributes.get("http.status_code"))
    assert status == 500
    assert server.status.status_code == trace.StatusCode.ERROR
    assert "boom" not in str(server.status.description)
    assert server.events == ()


async def test_unmatched_route_span_has_no_route(
    instrumented: GizeAPI, spans: InMemorySpanExporter
) -> None:
    async with _client(instrumented) as client:
        await client.get(f"/no/such/{PHONE}")
    (server,) = _server_spans(spans)
    assert server.name == "GET"
    assert "http.route" not in (server.attributes or {})
    for value in _attribute_values([server]):
        assert PHONE not in value


@pytest.mark.parametrize(
    ("path", "traced"),
    [("/health/live", False), ("/health/ready?x=1", False), ("/_test/health/live", True)],
)
async def test_only_the_health_endpoints_are_untraced(
    instrumented: GizeAPI, spans: InMemorySpanExporter, path: str, traced: bool
) -> None:
    async with _client(instrumented) as client:
        await client.get(path)
    assert bool(_server_spans(spans)) is traced


async def test_inbound_trace_context_from_clients_is_ignored(
    instrumented: GizeAPI, spans: InMemorySpanExporter
) -> None:
    inbound_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with _client(instrumented) as client:
        await client.get(
            "/_test/riders/1",
            headers={"traceparent": f"00-{inbound_trace}-00f067aa0ba902b7-00"},  # "not sampled"
        )
    (server,) = _server_spans(spans)  # traced anyway: clients cannot switch tracing off
    assert format(server.context.trace_id, "032x") != inbound_trace
    assert server.parent is None


async def test_sampler_ratio_zero_records_nothing(
    make_settings: SettingsFactory, spans: InMemorySpanExporter
) -> None:
    app = _app(make_settings)
    app.telemetry = configure_telemetry(
        make_settings(otel_traces_sampler_ratio=0.0), span_exporter=spans
    )
    async with _client(app) as client:
        await client.get("/_test/riders/1")
    assert spans.get_finished_spans() == ()


# --- metrics ----------------------------------------------------------------------------


async def test_request_metrics_recorded_with_bounded_attributes(
    instrumented: GizeAPI, metrics: InMemoryMetricReader
) -> None:
    for host in ("a.example", "b.example", "evil-host.example"):
        async with _client(instrumented, host=host) as client:
            await client.get("/_test/riders/1")
    data = metrics.get_metrics_data()
    assert data is not None
    points = {
        metric.name: [dict(point.attributes or {}) for point in metric.data.data_points]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    durations = next(
        v for k, v in points.items() if k.startswith("http.server") and "duration" in k
    )
    assert len(durations) == 1  # one series, whatever Host clients send
    for attributes in (a for series in points.values() for a in series):
        for key in attributes:
            assert "host" not in key
            assert "server_name" not in key
            assert "peer" not in key


# --- redis ------------------------------------------------------------------------------


async def test_redis_traced_per_client_and_untraced_after_shutdown(
    make_settings: SettingsFactory, spans: InMemorySpanExporter, test_redis_url: str
) -> None:
    app = _app(make_settings, redis_url=test_redis_url)
    app.telemetry = configure_telemetry(make_settings(), span_exporter=spans)
    async with app.router.lifespan_context(app):
        await app.state.redis.set("gize:test:key", "secret-value-0911223344", ex=5)
        async with Redis.from_url(test_redis_url) as other:
            await other.ping()  # a client the app did not create: not traced
    names = [span.name for span in spans.get_finished_spans()]
    assert names == ["SET"]
    assert "secret-value" not in str(_attribute_values(spans.get_finished_spans()))
    spans.clear()
    async with Redis.from_url(test_redis_url) as after:
        await after.ping()
    assert spans.get_finished_spans() == ()


# --- failures ---------------------------------------------------------------------------


class FailingExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector unreachable")


async def test_exporter_failure_does_not_affect_requests(make_settings: SettingsFactory) -> None:
    app = _app(make_settings)
    app.telemetry = configure_telemetry(make_settings(), span_exporter=FailingExporter())
    async with _client(app) as client:
        response = await client.get("/_test/riders/1")
    assert response.status_code == 200


async def test_shutdown_is_bounded_when_the_collector_hangs(
    make_settings: SettingsFactory, blackhole_url: str
) -> None:
    telemetry = configure_telemetry(make_settings(otel_exporter_otlp_endpoint=blackhole_url))
    assert telemetry is not None
    with telemetry.tracer_provider.get_tracer("test").start_as_current_span("pending"):
        pass
    started = time.monotonic()
    await telemetry.shutdown(budget_seconds=0.5)
    assert time.monotonic() - started < 2.0


async def test_lifespan_shuts_telemetry_down_even_if_redis_close_fails(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingExporter(InMemorySpanExporter):
        shut_down = False

        def shutdown(self) -> None:
            RecordingExporter.shut_down = True
            super().shutdown()

    async def failing_close(self: Redis) -> None:
        raise ConnectionError("close failed")

    monkeypatch.setattr(Redis, "aclose", failing_close)
    app = _app(make_settings)
    app.telemetry = configure_telemetry(make_settings(), span_exporter=RecordingExporter())
    async with app.router.lifespan_context(app):
        pass
    assert RecordingExporter.shut_down


# --- span scrubbing hook ----------------------------------------------------------------


def test_url_attributes_replaced_in_both_semantic_conventions() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span(
        "GET",
        attributes={
            "http.target": f"/v1/x/{PHONE}?phone={PHONE}",
            "http.url": f"https://api.example.com/v1/x/{PHONE}?phone={PHONE}",
            "url.full": f"https://api.example.com/v1/x/{PHONE}?phone={PHONE}",
            "url.path": f"/v1/x/{PHONE}",
            "url.query": f"phone={PHONE}",
            "http.host": "evil-host.example",
            "net.peer.ip": "10.9.8.7",
            "client.address": "10.9.8.7",
            "http.user_agent": "Mozilla/5.0 (Secret Phone)",
            "user_agent.original": "Mozilla/5.0 (Secret Phone)",
        },
    ) as span:
        _scrub_request_span(span, {"path": f"/v1/x/{PHONE}", "type": "http"})
    (finished,) = exporter.get_finished_spans()
    for value in _attribute_values([finished]):
        for sensitive in SENSITIVE:
            assert sensitive not in value


def test_scrub_hook_ignores_non_recording_spans() -> None:
    _scrub_request_span(INVALID_SPAN, {"path": "/x"})  # must not raise


async def test_metrics_only_configuration(make_settings: SettingsFactory) -> None:
    telemetry = configure_telemetry(make_settings(), metric_reader=InMemoryMetricReader())
    assert telemetry is not None
    await telemetry.shutdown()


@pytest.mark.parametrize(
    ("scope", "expected_headers"),
    [
        ({"type": "lifespan"}, None),
        ({"type": "http", "headers": [(b"accept", b"*/*")]}, [(b"accept", b"*/*")]),
        (
            {
                "type": "http",
                "headers": [(b"traceparent", b"00-x"), (b"Baggage", b"k=v"), (b"a", b"b")],
            },
            [(b"a", b"b")],
        ),
    ],
)
async def test_inbound_trace_headers_dropped_only_from_http(
    scope: Scope, expected_headers: list[tuple[bytes, bytes]] | None
) -> None:
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        return None

    await _DropInboundTraceContext(inner)(scope, receive, send)
    assert seen[0].get("headers") == expected_headers


def test_process_exit_is_bounded_when_the_collector_hangs(blackhole_url: str) -> None:
    """The await returning is not enough: the process must also exit within the budget."""
    script = f"""
import asyncio
from gize_api.core.config import Settings
from gize_api.core.telemetry import configure_telemetry

settings = Settings(app_env="test", redis_url="redis://127.0.0.1:1/0",
                    otel_exporter_otlp_endpoint="{blackhole_url}")
telemetry = configure_telemetry(settings)
with telemetry.tracer_provider.get_tracer("t").start_as_current_span("pending"):
    pass
telemetry.meter_provider.get_meter("t").create_counter("c").add(1)
asyncio.run(telemetry.shutdown(budget_seconds=1.0))
"""
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)  # noqa: S603
    assert time.monotonic() - started < 4.0


async def test_unknown_methods_named_other(
    instrumented: GizeAPI, spans: InMemorySpanExporter
) -> None:
    async with _client(instrumented) as client:
        await client.request("FOO", "/_test/riders/1")
    (server,) = _server_spans(spans)
    assert not server.name.startswith("FOO")


def test_captured_http_headers_are_blanked() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span(
        "GET",
        attributes={
            "http.request.header.authorization": ("Bearer x",),
            "http.response.header.set_cookie": ("s=1",),
        },
    ) as span:
        _scrub_request_span(span, {"path": "/x", "type": "http"})
    (finished,) = exporter.get_finished_spans()
    assert all(not value for value in (finished.attributes or {}).values())


async def test_resolving_a_finished_shutdown_future_is_harmless() -> None:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _resolve(future)
    _resolve(future)  # a second resolution (late thread) must not raise
    assert future.done()
