"""OpenTelemetry traces and metrics, exported over OTLP/HTTP (Grafana Cloud).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (local runs and tests need nothing). Request
spans are named by route template; query strings never reach span attributes and phone
numbers in paths are masked. Redis commands are recorded as `COMMAND ? ?`, never values.
SQLAlchemy (GZ-006) and outbound httpx (GZ-008) instrumentation join when those land.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Scope

from gize_api.core.config import Settings
from gize_api.core.logging import mask_phone_numbers

EXPORT_TIMEOUT_SECONDS = 10
METRIC_EXPORT_INTERVAL_MS = 60_000
EXCLUDED_URLS = "/health/"

# Attributes the ASGI instrumentation fills from the raw URL (old and new semantic conventions).
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.path", "url.query")


@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def wrap(self, app: ASGIApp, routes: Sequence[BaseRoute]) -> ASGIApp:
        """Trace and measure every request, outermost in the stack (see main.GizeAPI).

        The FastAPI instrumentor is not used: it requires Starlette's ServerErrorMiddleware
        outermost (ours wrap it) and copies exception messages into span status. Spans
        here are named by route template and a 5xx marks them as errors, with no messages.
        """
        return OpenTelemetryMiddleware(
            app,
            excluded_urls=EXCLUDED_URLS,
            default_span_details=_span_details(routes),
            server_request_hook=_scrub_request_span,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            exclude_spans=["receive", "send"],
        )

    def shutdown(self) -> None:
        """Flush and stop exporters (bounded by their timeouts); undo global patches."""
        RedisInstrumentor().uninstrument()
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def configure_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry | None:
    """Set up providers and Redis tracing, or return None when nothing would export.

    `span_exporter` and `metric_reader` replace the OTLP exporters (tests use in-memory ones).
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if endpoint is None and span_exporter is None and metric_reader is None:
        return None
    headers = settings.otlp_headers()

    attributes: dict[str, str] = {
        "service.name": settings.otel_service_name,
        "deployment.environment.name": settings.app_env.value,
    }
    if settings.release is not None:
        attributes["service.version"] = settings.release
    resource = Resource.create(attributes)

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif endpoint is not None:
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint}/v1/traces", headers=headers, timeout=EXPORT_TIMEOUT_SECONDS
        )
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    readers: list[MetricReader] = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif endpoint is not None:
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics", headers=headers, timeout=EXPORT_TIMEOUT_SECONDS
        )
        readers.append(
            PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
                export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
            )
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)

    RedisInstrumentor().instrument(tracer_provider=tracer_provider)
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


def _span_details(
    routes: Sequence[BaseRoute],
) -> Callable[[Scope], tuple[str, dict[str, str]]]:
    """Name request spans `GET /v1/riders/{rider_id}`: the template, never the raw path."""

    def details(scope: Scope) -> tuple[str, dict[str, str]]:
        method = str(scope.get("method", "HTTP"))
        for route in routes:
            match, _ = route.matches(scope)
            path = getattr(route, "path", None)
            if match == Match.FULL and isinstance(path, str):
                return f"{method} {path}", {"http.route": path}
        return method, {}

    return details


def _scrub_request_span(span: Span, scope: dict[str, Any]) -> None:
    """Replace URL attributes: drop the query string, mask phone numbers in the path."""
    if not isinstance(span, ReadableSpan) or not span.is_recording():
        return
    present = span.attributes or {}
    path = mask_phone_numbers(str(scope.get("path", "")))
    for name in _URL_ATTRIBUTES:
        if name not in present:
            continue
        if name == "url.query":
            span.set_attribute(name, "")
        elif name in ("http.url", "url.full"):
            span.set_attribute(name, _without_query(str(present[name]), path))
        else:
            span.set_attribute(name, path)


def _without_query(url: str, masked_path: str) -> str:
    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}{masked_path}"
