"""OpenTelemetry traces, metrics and logs, exported over OTLP/HTTP (Grafana Cloud).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (local runs and tests need nothing).

- Request spans are named by route template (the one the app chose, after routing). URL
  attributes carry the template, never the raw path or query; client address, user agent
  and Host are blanked.
- Metrics keep only method, status and route: client-controlled values (Host) would let
  anyone create unbounded series.
- Trace context sent by clients is ignored (they are the public internet); new traces are
  sampled at OTEL_TRACES_SAMPLER_RATIO.
- Redis commands are traced per client, as `COMMAND ? ?`, never values.
- Shutdown flushes within a fixed budget, off the event loop.

- Database statements are traced per engine as their operation and their text with every
  literal replaced by `?`, never parameters or error text, with per-operation duration and
  error metrics. Requests failed by an unavailable database are counted.

- Logs (GZ-066): every line written to stderr is shipped as it was written, scrubbed
  (core/logging.py), its fields as attributes and its event as the body, linked to the
  trace it was logged in. Shipping starts with `ship_logs()`, once the app is built.

Outbound httpx instrumentation (GZ-008) joins when that client lands.
"""

import asyncio
import contextlib
import functools
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gize_api.core.config import Settings
from gize_api.core.logging import known_method, log_export, set_log_export

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
# Set only if OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_* is configured; blanked regardless.
_CAPTURED_HEADER_PREFIXES = ("http.request.header.", "http.response.header.")
MAX_STATEMENT_LENGTH = 1000
# Literal stripping looks at this much of a statement; the rest is dropped unread.
_MAX_STATEMENT_SCAN = 4 * MAX_STATEMENT_LENGTH
_SQL_LITERAL = re.compile(
    r"--[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r"|(?<![\w$])[Ee]'(?:[^'\\]|''|\\.)*'"  # escape string: backslash escapes
    r"|(?<![\w$])(?:[BbXx]|[Uu]&)?'(?:[^']|'')*'"  # string, bit string, Unicode string
    r"|\$\$.*?\$\$"  # dollar-quoted string
    r"|\$(?P<tag>[A-Za-z_]\w*)\$.*?\$(?P=tag)\$"  # tagged dollar-quoted string ($1 is not)
    r"|'.*|\$(?:[A-Za-z_]\w*)?\$.*"  # unterminated (a truncated or broken statement): to the end
    r"|(?<![\w$])(?:0[xXoObB][0-9A-Fa-f_]+|\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
    re.DOTALL,
)
_DATABASE_OPERATIONS = frozenset(
    {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "WITH",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "SHOW",
        "SET",
    }
)
_TRACE_CONTEXT_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})

logger = structlog.get_logger("gize_api.telemetry")


_SEVERITY = (
    (logging.CRITICAL, SeverityNumber.FATAL),
    (logging.ERROR, SeverityNumber.ERROR),
    (logging.WARNING, SeverityNumber.WARN),
    (logging.INFO, SeverityNumber.INFO),
)


@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None = None

    def ship_logs(self) -> None:
        """Ship every line written from now on (core/logging.py), until shutdown."""
        if self.logger_provider is not None:
            set_log_export(self._ship)

    def stop_shipping_logs(self) -> None:
        """Stop shipping, if these are the lines' destination (another app's are left be)."""
        if log_export() == self._ship:
            set_log_export(None)

    def _ship(self, record: logging.LogRecord, line: str) -> None:
        fields = json.loads(line)
        body = fields.pop("event", None)
        self._log_emitter.emit(
            timestamp=int(record.created * 1e9),
            severity_number=next(
                (number for level, number in _SEVERITY if record.levelno >= level),
                SeverityNumber.DEBUG,
            ),
            severity_text=record.levelname,
            body=body if isinstance(body, str) else line,
            attributes=_log_attributes(fields),
        )

    @functools.cached_property
    def _log_emitter(self) -> Any:  # noqa: ANN401 - the SDK's Logger
        assert self.logger_provider is not None  # noqa: S101 - only shipped with one
        return self.logger_provider.get_logger("gize_api")

    def wrap(self, app: ASGIApp, routes: Sequence[BaseRoute]) -> ASGIApp:
        """Trace and measure every request, outermost in the stack (see main.GizeAPI).

        The FastAPI instrumentor is not used: it requires Starlette's ServerErrorMiddleware
        outermost (ours wrap it) and copies exception messages into span status. Here a 5xx
        marks the span as an error from the status code alone.
        """
        traced = OpenTelemetryMiddleware(
            _NameSpanByRoute(app),
            excluded_urls=EXCLUDED_URLS,
            default_span_details=_span_details(routes),
            server_request_hook=lambda span, scope: _scrub_request_span(span, scope, routes),
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            exclude_spans=["receive", "send"],
        )
        return _DropInboundTraceContext(traced)

    def instrument_database(self, engine: AsyncEngine) -> None:
        """One CLIENT span per statement and per-operation duration and error metrics.

        Written here instead of using the SQLAlchemy instrumentation package, which copies
        the exception text (Postgres DETAIL lines carry row values) into span status. Spans
        record the operation and the statement with its placeholders, never parameters.
        """
        tracer = self.tracer_provider.get_tracer("gize_api.db")
        duration = self.meter_provider.get_meter("gize_api.db").create_histogram(
            "db.client.operation.duration",
            unit="s",
            description="Duration of database statements",
        )

        def before(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy event signature
            statement, context = args[2], args[4]
            operation = _database_operation(statement)
            span = tracer.start_span(
                operation,
                kind=SpanKind.CLIENT,
                attributes={
                    "db.system.name": "postgresql",
                    "db.operation.name": operation,
                    "db.query.text": _statement_text(str(statement)),
                },
            )
            context._gize_db = (span, time.perf_counter(), operation)

        def finish(context: Any, error: BaseException | None) -> None:  # noqa: ANN401
            started = getattr(context, "_gize_db", None)
            if started is None:
                return
            span, began, operation = started
            context._gize_db = None
            outcome = "ok" if error is None else "error"
            if error is not None:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR))  # no description: no message text
            span.end()
            duration.record(
                time.perf_counter() - began,
                {
                    "db.system.name": "postgresql",
                    "db.operation.name": operation,
                    "outcome": outcome,
                },
            )

        def after(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy event signature
            finish(args[4], None)

        def on_error(exception_context: Any) -> None:  # noqa: ANN401 - SQLAlchemy event
            if exception_context.execution_context is not None:
                finish(exception_context.execution_context, exception_context.original_exception)

        sync_engine = engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", before)
        event.listen(sync_engine, "after_cursor_execute", after)
        event.listen(sync_engine, "handle_error", on_error)

    def record_database_unavailable(self, error: BaseException) -> None:
        """Count a request failed by an unavailable database (connection failures included)."""
        self._database_unavailable.add(1, {"error.type": type(error).__name__})

    @functools.cached_property
    def _database_unavailable(self) -> Counter:
        return self.meter_provider.get_meter("gize_api.db").create_counter(
            "gize.db.unavailable",
            description="Requests answered 503 because the database was unavailable",
        )

    def record_dependency_unavailable(self, dependency: str, error: BaseException) -> None:
        """Count a failed readiness check of `dependency` (a fixed name: postgres, redis)."""
        self._dependency_unavailable.add(
            1, {"dependency": dependency, "error.type": type(error).__name__}
        )

    def record_outbound_duration(self, seconds: float, attributes: dict[str, str | int]) -> None:
        """One outbound call (core/http.py): host, method and status or error type."""
        self._outbound_duration.record(seconds, attributes)

    @functools.cached_property
    def _outbound_duration(self) -> Histogram:
        return self.meter_provider.get_meter("gize_api.http").create_histogram(
            "http.client.request.duration",
            unit="s",
            description="Duration of calls to outside services",
        )

    def record_job(self, job: str, outcome: str, seconds: float) -> None:
        """One run of a background job: name, outcome and duration."""
        self._job_duration.record(seconds, {"job": job, "outcome": outcome})

    @functools.cached_property
    def _job_duration(self) -> Histogram:
        return self.meter_provider.get_meter("gize_api.worker").create_histogram(
            "gize.job.duration", unit="s", description="Duration of background jobs by outcome"
        )

    def record_sms(self, purpose: str, outcome: str, seconds: float) -> None:
        """One SMS send (sms.service): purpose, outcome (sent, duplicate, or the failure
        kind) and how long the sender took. Never the phone or the text."""
        self._sms_duration.record(seconds, {"purpose": purpose, "outcome": outcome})

    @functools.cached_property
    def _sms_duration(self) -> Histogram:
        return self.meter_provider.get_meter("gize_api.sms").create_histogram(
            "gize.sms.send.duration", unit="s", description="SMS sends by purpose and outcome"
        )

    def record_redis_operation(self, operation: str, outcome: str, seconds: float) -> None:
        """One Redis operation of ours (rate_limit, enqueue, ping): its outcome and duration.

        The Redis instrumentation records spans only; every place we call Redis records this.
        """
        self._redis_duration.record(
            seconds,
            {"db.system.name": "redis", "db.operation.name": operation, "outcome": outcome},
        )

    @functools.cached_property
    def _redis_duration(self) -> Histogram:
        return self.meter_provider.get_meter("gize_api.redis").create_histogram(
            "db.client.operation.duration", unit="s", description="Duration of Redis operations"
        )

    def record_health_probe(self, endpoint: str, status: int) -> None:
        """Count a probe (`live` or `ready`): probes are neither traced nor access-logged."""
        self._health_probes.add(1, {"endpoint": endpoint, "status": status})

    @functools.cached_property
    def _health_probes(self) -> Counter:
        return self.meter_provider.get_meter("gize_api.health").create_counter(
            "gize.health.probes", description="Requests to /health/live and /health/ready"
        )

    @functools.cached_property
    def _dependency_unavailable(self) -> Counter:
        return self.meter_provider.get_meter("gize_api.health").create_counter(
            "gize.dependency.unavailable",
            description="Readiness checks that found a dependency unavailable",
        )

    def instrument_redis(self, client: Redis) -> None:
        RedisInstrumentor.instrument_client(client, tracer_provider=self.tracer_provider)

    def uninstrument_redis(self, client: Redis) -> None:
        RedisInstrumentor.uninstrument_client(client)

    async def shutdown(self, budget_seconds: float = SHUTDOWN_BUDGET_SECONDS) -> None:
        """Flush and stop exporters within one overall deadline, off the event loop.

        Anything not exported by then is dropped: shutdown must fit inside the container's
        stop timeout together with the 30 s request drain. The work runs in a daemon thread,
        so a hung collector cannot hold the process open after the deadline either.
        """
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        deadline = time.monotonic() + budget_seconds

        def run() -> None:
            try:
                self._shutdown_until(deadline)
            finally:
                with contextlib.suppress(RuntimeError):  # loop already closed
                    loop.call_soon_threadsafe(_resolve, finished)

        threading.Thread(target=run, name="telemetry-shutdown", daemon=True).start()
        try:
            await asyncio.wait_for(asyncio.shield(finished), timeout=budget_seconds)
        except TimeoutError:
            logger.warning("telemetry_shutdown_timed_out", budget_seconds=budget_seconds)

    def _shutdown_until(self, deadline: float) -> None:
        """Flush what time allows, then always shut both providers down; never raise."""

        def remaining_ms() -> int:
            return max(0, int((deadline - time.monotonic()) * 1000))

        self.stop_shipping_logs()
        steps: list[tuple[str, Callable[[], object]]] = [
            ("trace_flush", lambda: self.tracer_provider.force_flush(remaining_ms())),
            ("metric_flush", lambda: self.meter_provider.force_flush(remaining_ms())),
            ("trace_shutdown", self.tracer_provider.shutdown),
            ("metric_shutdown", lambda: self.meter_provider.shutdown(max(1, remaining_ms()))),
        ]
        logs = self.logger_provider
        if logs is not None:
            # Last: the SDK's log flush ignores its timeout, so it must not use up the
            # budget before traces and metrics are out.
            steps.append(("log_flush", lambda: logs.force_flush(remaining_ms())))
            steps.append(("log_shutdown", logs.shutdown))
        for name, step in steps:
            if name.endswith("_flush") and remaining_ms() == 0:
                continue
            try:
                step()
            except Exception:
                logger.warning("telemetry_shutdown_step_failed", step=name, exc_info=True)


def configure_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    log_exporter: LogRecordExporter | None = None,
) -> Telemetry | None:
    """Set up providers, or return None when nothing would be exported.

    `span_exporter`, `metric_reader` and `log_exporter` replace the OTLP exporters (tests
    use in-memory ones). Logs are shipped only once `ship_logs()` is called.
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if (
        endpoint is None
        and span_exporter is None
        and metric_reader is None
        and (log_exporter is None)
    ):
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
        # Exemplars would carry the request's raw attributes (Host) that the View removes.
        exemplar_filter=AlwaysOffExemplarFilter(),
        metric_readers=readers,
        views=[View(instrument_name="http.server.*", attribute_keys=set(_METRIC_ATTRIBUTES))],
        shutdown_on_exit=False,
    )
    logger_provider: LoggerProvider | None = None
    if log_exporter is not None or endpoint is not None:
        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        if log_exporter is not None:
            logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        else:
            otlp_logs = OTLPLogExporter(
                endpoint=f"{endpoint}/v1/logs", headers=headers, timeout=EXPORT_TIMEOUT_SECONDS
            )
            # Each export is bounded by the exporter's own timeout (the processor's
            # export_timeout_millis is unused in SDK 1.45).
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(otlp_logs))
    return Telemetry(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )


def _log_attributes(fields: dict[str, Any]) -> dict[str, Any]:
    """A shipped line's fields as attributes: scalars and lists of text as they are, other
    shapes as compact JSON, nulls left out (the timestamp is the record's own)."""
    attributes: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or key == "timestamp":
            continue
        if isinstance(value, bool | float | str) or (
            isinstance(value, int) and -(2**63) <= value < 2**63
        ):
            attributes[key] = value  # OTLP integers are 64-bit: a bigger one goes as JSON
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            attributes[key] = tuple(value)
        else:
            attributes[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return attributes


def _statement_text(statement: str) -> str:
    """The statement with comments and literals replaced by `?` (placeholders such as `$1`
    stay), bounded in length: values must never reach a span, even in a literal."""
    return _SQL_LITERAL.sub("?", statement[:_MAX_STATEMENT_SCAN])[:MAX_STATEMENT_LENGTH]


def _database_operation(statement: object) -> str:
    """The statement's first keyword from a fixed list (bounded span names and labels)."""
    words = str(statement).lstrip().split(None, 1)
    keyword = words[0].upper() if words else ""
    return keyword if keyword in _DATABASE_OPERATIONS else "OTHER"


def _resolve(future: "asyncio.Future[None]") -> None:
    if not future.done():
        future.set_result(None)


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


class _NameSpanByRoute:
    """Name the request span after the route the app chose (GZ-066).

    FastAPI 0.162 wraps included routers (`_IncludedRouter`), so matching `app.routes`
    before routing finds only routes added on the app itself: the app's own requests were
    named `GET`. FastAPI records the chosen route in the scope; when the response starts
    (routing done, and before the instrumentation ends the span with the response's last
    part), the span takes its template, as the request metrics already do.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        span = trace.get_current_span()

        async def named(message: Message) -> None:
            if message["type"] == "http.response.start":
                _name_by_route(span, scope)
            await send(message)

        await self.app(scope, receive, named)


def _name_by_route(span: Span, scope: Scope) -> None:
    template = getattr(scope.get("route"), "path_format", None)
    if not isinstance(template, str) or not span.is_recording():
        return
    span.update_name(f"{known_method(str(scope.get('method', 'HTTP')))} {template}")
    span.set_attribute("http.route", template)
    present = span.attributes if isinstance(span, ReadableSpan) else None
    for name in _URL_ATTRIBUTES:
        if name in (present or {}) and name != "url.query":
            span.set_attribute(name, template)


def _span_details(
    routes: Sequence[BaseRoute],
) -> Callable[[Scope], tuple[str, dict[str, str]]]:
    """Name request spans `GET /v1/riders/{rider_id}`: the template, never the raw path."""

    def details(scope: Scope) -> tuple[str, dict[str, str]]:
        method = known_method(str(scope.get("method", "HTTP")))
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
    for name in present:
        if name in _CLIENT_ATTRIBUTES or name.startswith(_CAPTURED_HEADER_PREFIXES):
            span.set_attribute(name, "")
