"""OpenTelemetry traces and metrics, exported over OTLP/HTTP (Grafana Cloud).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (local runs and tests need nothing).

- Request spans are named by route template. URL attributes carry the template, never the
  raw path or query; client address, user agent and Host are blanked.
- Metrics keep only method, status and route: client-controlled values (Host) would let
  anyone create unbounded series.
- Trace context sent by clients is ignored (they are the public internet); new traces are
  sampled at OTEL_TRACES_SAMPLER_RATIO.
- Redis commands are traced per client, as `COMMAND ? ?`, never values.
- Shutdown flushes within a fixed budget, off the event loop.

SQLAlchemy (GZ-006) and outbound httpx (GZ-008) instrumentation join when those land.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span
from redis.asyncio import Redis
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

from gize_api.core.config import Settings

EXPORT_TIMEOUT_SECONDS = 5
SHUTDOWN_BUDGET_SECONDS = 5.0
METRIC_EXPORT_INTERVAL_MS = 60_000
# Matched against the full request URL; only the two health endpoints are untraced.
EXCLUDED_URLS = r"^https?://[^/]+/health/(live|ready)(\?.*)?$"

_METRIC_ATTRIBUTES = frozenset(
    {
        "http.method",
        "http.status_code",
        "http.route",
        "http.target",  # the route template on metrics
        "http.scheme",
        "http.request.method",
        "http.response.status_code",
        "url.scheme",
    }
)
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.path", "url.query")
_CLIENT_ATTRIBUTES = (
    "net.peer.ip",
    "net.peer.port",
    "net.peer.name",
    "client.address",
    "client.port",
    "http.client_ip",
    "http.user_agent",
    "user_agent.original",
    "http.host",
    "http.server_name",
    "net.host.name",
    "net.host.port",
    "server.address",
    "server.port",
)
_TRACE_CONTEXT_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})

logger = structlog.get_logger("gize_api.telemetry")


@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def wrap(self, app: ASGIApp, routes: Sequence[BaseRoute]) -> ASGIApp:
        """Trace and measure every request, outermost in the stack (see main.GizeAPI).

        The FastAPI instrumentor is not used: it requires Starlette's ServerErrorMiddleware
        outermost (ours wrap it) and copies exception messages into span status. Here a 5xx
        marks the span as an error from the status code alone.
        """
        traced = OpenTelemetryMiddleware(
            app,
            excluded_urls=EXCLUDED_URLS,
            default_span_details=_span_details(routes),
            server_request_hook=lambda span, scope: _scrub_request_span(span, scope, routes),
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            exclude_spans=["receive", "send"],
        )
        return _DropInboundTraceContext(traced)

    def instrument_redis(self, client: Redis) -> None:
        RedisInstrumentor.instrument_client(client, tracer_provider=self.tracer_provider)

    def uninstrument_redis(self, client: Redis) -> None:
        RedisInstrumentor.uninstrument_client(client)

    async def shutdown(self, budget_seconds: float = SHUTDOWN_BUDGET_SECONDS) -> None:
        """Flush and stop exporters within `budget_seconds`, without blocking the event loop.

        Anything not exported by then is dropped: shutdown must fit inside the container's
        stop timeout together with the 30 s request drain.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._shutdown_now, budget_seconds), timeout=budget_seconds
            )
        except TimeoutError:
            logger.warning("telemetry_shutdown_timed_out", budget_seconds=budget_seconds)

    def _shutdown_now(self, budget_seconds: float) -> None:
        budget_ms = int(budget_seconds * 1000)
        self.tracer_provider.force_flush(timeout_millis=budget_ms)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown(timeout_millis=budget_ms)


def configure_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry | None:
    """Set up providers, or return None when nothing would be exported.

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

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
        shutdown_on_exit=False,  # the lifespan owns shutdown
    )
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
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=readers,
        views=[View(instrument_name="http.server.*", attribute_keys=set(_METRIC_ATTRIBUTES))],
        shutdown_on_exit=False,
    )
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


class _DropInboundTraceContext:
    """Remove trace headers sent by clients, so every request starts a trace of our own."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [h for h in scope["headers"] if h[0].lower() not in _TRACE_CONTEXT_HEADERS]
            if len(headers) != len(scope["headers"]):
                scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def _route_template(routes: Sequence[BaseRoute], scope: Scope) -> str | None:
    for route in routes:
        match, _ = route.matches(scope)
        path = getattr(route, "path", None)
        if match == Match.FULL and isinstance(path, str):
            return path
    return None


def _span_details(
    routes: Sequence[BaseRoute],
) -> Callable[[Scope], tuple[str, dict[str, str]]]:
    """Name request spans `GET /v1/riders/{rider_id}`: the template, never the raw path."""

    def details(scope: Scope) -> tuple[str, dict[str, str]]:
        method = str(scope.get("method", "HTTP"))
        template = _route_template(routes, scope)
        if template is None:
            return method, {}
        return f"{method} {template}", {"http.route": template}

    return details


def _scrub_request_span(
    span: Span, scope: dict[str, Any], routes: Sequence[BaseRoute] = ()
) -> None:
    """Replace URL attributes with the route template; blank client-identifying ones."""
    if not isinstance(span, ReadableSpan) or not span.is_recording():
        return
    present = span.attributes or {}
    template = _route_template(routes, scope) if scope.get("type") == "http" else None
    for name in _URL_ATTRIBUTES:
        if name in present:
            span.set_attribute(name, "" if name == "url.query" else template or "unmatched")
    for name in _CLIENT_ATTRIBUTES:
        if name in present:
            span.set_attribute(name, "")
