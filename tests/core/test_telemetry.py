"""GZ-005: OpenTelemetry traces and metrics; off unless configured; never leaks queries."""

from collections.abc import Iterator, Sequence

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN

from gize_api.core.telemetry import Telemetry, _scrub_request_span, configure_telemetry
from gize_api.main import create_app
from tests.support import SettingsFactory

PHONE = "0911223344"


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings())

    @app.get("/_test/riders/{rider_id}")
    async def rider(rider_id: str) -> dict[str, str]:
        return {"rider_id": rider_id}

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("boom")

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
) -> Iterator[tuple[FastAPI, Telemetry]]:
    app = _app(make_settings)
    telemetry = configure_telemetry(make_settings(), span_exporter=spans, metric_reader=metrics)
    assert telemetry is not None
    app.telemetry = telemetry  # type: ignore[attr-defined]  # GizeAPI attribute
    yield app, telemetry
    telemetry.shutdown()  # undo the global Redis instrumentation


def _all_attribute_values(finished: Sequence[ReadableSpan]) -> list[str]:
    return [str(v) for span in finished for v in (span.attributes or {}).values()]


def test_disabled_without_an_endpoint(make_settings: SettingsFactory) -> None:
    assert configure_telemetry(make_settings()) is None


async def test_request_span_named_by_route_template_without_query(
    instrumented: tuple[FastAPI, Telemetry], spans: InMemorySpanExporter
) -> None:
    app, _ = instrumented
    async with _client(app) as client:
        response = await client.get(f"/_test/riders/{PHONE}", params={"phone": PHONE})
    assert response.status_code == 200
    finished = spans.get_finished_spans()
    server = [s for s in finished if s.name == "GET /_test/riders/{rider_id}"]
    assert len(server) == 1
    for value in _all_attribute_values(finished):
        assert PHONE not in value
        assert "phone=" not in value


async def test_500_is_recorded_as_server_error(
    instrumented: tuple[FastAPI, Telemetry], spans: InMemorySpanExporter
) -> None:
    app, _ = instrumented
    async with _client(app) as client:
        await client.get("/_test/crash")
    (server,) = [s for s in spans.get_finished_spans() if s.name == "GET /_test/crash"]
    assert (server.attributes or {}).get(
        "http.response.status_code", (server.attributes or {}).get("http.status_code")
    ) == 500
    assert "boom" not in str([e.attributes for e in server.events])


async def test_request_metrics_recorded(
    instrumented: tuple[FastAPI, Telemetry], metrics: InMemoryMetricReader
) -> None:
    app, _ = instrumented
    async with _client(app) as client:
        await client.get("/_test/riders/1")
    data = metrics.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert any(name.startswith("http.server") and "duration" in name for name in names)


async def test_health_paths_are_not_traced(
    instrumented: tuple[FastAPI, Telemetry], spans: InMemorySpanExporter
) -> None:
    app, _ = instrumented
    async with _client(app) as client:
        await client.get("/health/live")
    assert spans.get_finished_spans() == ()


class FailingExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector unreachable")


async def test_exporter_failure_does_not_affect_requests(
    make_settings: SettingsFactory,
) -> None:
    app = _app(make_settings)
    app.telemetry = configure_telemetry(  # type: ignore[attr-defined]  # GizeAPI attribute
        make_settings(), span_exporter=FailingExporter()
    )
    async with _client(app) as client:
        response = await client.get("/_test/riders/1")
    assert response.status_code == 200


async def test_lifespan_shutdown_flushes_telemetry(make_settings: SettingsFactory) -> None:
    class RecordingExporter(InMemorySpanExporter):
        shut_down = False

        def shutdown(self) -> None:
            RecordingExporter.shut_down = True
            super().shutdown()

    app = _app(make_settings)
    app.telemetry = configure_telemetry(  # type: ignore[attr-defined]  # GizeAPI attribute
        make_settings(), span_exporter=RecordingExporter()
    )
    async with app.router.lifespan_context(app):
        pass
    assert RecordingExporter.shut_down


def test_otlp_exporters_configured_from_settings(make_settings: SettingsFactory) -> None:
    # A refused loopback port: nothing leaves the machine, and shutdown still completes.
    settings = make_settings(
        otel_exporter_otlp_endpoint="http://127.0.0.1:1",
        otel_exporter_otlp_headers="Authorization=Basic eA==",
        release="2026.10.27-abc123",
    )
    telemetry = configure_telemetry(settings)
    assert telemetry is not None
    resource = telemetry.tracer_provider.resource.attributes
    assert resource["service.name"] == "gize-admin-api"
    assert resource["service.version"] == "2026.10.27-abc123"
    assert resource["deployment.environment.name"] == "test"
    telemetry.shutdown()


def test_url_attributes_scrubbed_in_both_semantic_conventions() -> None:
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
        },
    ) as span:
        _scrub_request_span(span, {"path": f"/v1/x/{PHONE}"})
    (finished,) = exporter.get_finished_spans()
    assert dict(finished.attributes or {}) == {
        "http.target": "/v1/x/09••••3344",
        "http.url": "https://api.example.com/v1/x/09••••3344",
        "url.full": "https://api.example.com/v1/x/09••••3344",
        "url.path": "/v1/x/09••••3344",
        "url.query": "",
    }


def test_scrub_hook_ignores_non_recording_spans() -> None:
    _scrub_request_span(INVALID_SPAN, {"path": "/x"})  # must not raise


async def test_unmatched_route_span_has_no_route(
    instrumented: tuple[FastAPI, Telemetry], spans: InMemorySpanExporter
) -> None:
    app, _ = instrumented
    async with _client(app) as client:
        await client.get(f"/no/such/{PHONE}")
    (server,) = spans.get_finished_spans()
    assert server.name == "GET"
    assert "http.route" not in (server.attributes or {})
    for value in _all_attribute_values([server]):
        assert PHONE not in value


def test_metrics_only_configuration(make_settings: SettingsFactory) -> None:
    telemetry = configure_telemetry(make_settings(), metric_reader=InMemoryMetricReader())
    assert telemetry is not None
    telemetry.shutdown()
