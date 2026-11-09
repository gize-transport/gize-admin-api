"""Structured JSON logs (structlog) with request context and PII scrubbing.

Every line is one JSON object: timestamp (UTC), level, logger, event, request_id, trace_id
(inside a span) and, once authenticated, staff_id. Standard-library loggers (uvicorn,
libraries, warnings) go through the same pipeline. Scrubbing runs last, after exceptions
over every value of every type (core/scrub.py): secrets and personal data are replaced by
`[REDACTED]`, phone numbers are masked as `+2519••••1234`.

Each line, once written, can also be shipped (GZ-066: core/telemetry.py sends it to Grafana
Cloud over OTLP): the very line stderr got, scrubbed. The OpenTelemetry libraries' own lines
stay on stderr only, so an export that fails cannot feed itself.

Exceptions are logged as `exception_type`, `exception_chain` and `exception_frames`
(`file:line in function`), never their message or source lines: messages carry whatever
data the failing code held (names, phones, codes), which no pattern can reliably catch.
"""

import contextlib
import json
import logging
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any, TextIO

import sentry_sdk
import structlog
from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, Processor, WrappedLogger

from gize_api.core.request_id import current_request_id
from gize_api.core.scrub import (
    MAX_TEXT_LENGTH,
    REDACTED,
    TRUNCATED,
    Budget,
    exception_type_name,
    mask_phone_numbers,
    scrub_text,
    scrub_value,
)

__all__ = [
    "MAX_TEXT_LENGTH",
    "REDACTED",
    "TRUNCATED",
    "AccessLogMiddleware",
    "configure_logging",
    "current_staff_id",
    "known_method",
    "log_security_event",
    "mask_phone_numbers",
    "scrub_text",
    "scrub_value",
    "set_log_export",
    "set_staff_id",
]

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
MAX_FRAMES = 30
MAX_CHAIN = 5

_staff_id: ContextVar[str | None] = ContextVar("staff_id", default=None)

LogExport = Callable[[logging.LogRecord, str], None]
# Where each written line is also sent (core/telemetry.py), or None.
_export: LogExport | None = None
# Loggers whose lines are never shipped: the exporters' own (an export failing, retrying).
_NOT_SHIPPED = frozenset({"opentelemetry", "urllib3", "requests"})


def set_log_export(export: LogExport | None) -> None:
    """Send every line written from now on to `export` too (None: stop)."""
    global _export  # one process-wide pipeline, like the root handler
    _export = export


def log_export() -> LogExport | None:
    """Where lines are shipped now, if anywhere."""
    return _export


def set_staff_id(staff_id: str | None) -> None:
    """Attach the authenticated staff member to the rest of this request's log lines.

    Call from an async dependency (it shares the request's context); AccessLogMiddleware
    clears it for every new request.
    """
    _staff_id.set(staff_id)


def current_staff_id() -> str | None:
    """The staff member set by the auth dependency for this request, if any."""
    return _staff_id.get()


def log_security_event(
    event: str,
    *,
    sentry: bool = True,
    **fields: Any,  # noqa: ANN401 - log fields
) -> None:
    """Failed auth, permission denials, rate-limit hits, OTP abuse: logged at WARNING and
    sent to Sentry as a warning event (the alert route until logs are shipped, GZ-066).

    `sentry=False` logs only: for events that can come in floods (rate-limit hits), the
    caller sends one now and then rather than every one.
    """
    structlog.get_logger("gize_api.security").warning(event, category="security", **fields)
    if not sentry:
        return
    try:
        budget = Budget()
        extras = {key: scrub_value(key, value, budget=budget) for key, value in fields.items()}
    except Exception:  # noqa: BLE001 - reporting must never break the request it reports on
        extras = {"scrub": "failed"}
    sentry_sdk.capture_message(
        event,
        level="warning",
        tags={"category": "security", "request_id": current_request_id() or "none"},
        extras=extras,
    )


def configure_logging(level: str) -> None:
    """Route structlog, the standard library and warnings through one JSON pipeline."""
    shared: list[Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_request_context,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            _exceptions_to_types,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _format_exception,
            _scrub,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = _CurrentStderrHandler()
    handler.addFilter(_StdlibRecordFilter())
    handler.setFormatter(formatter)

    # The root logger gets exactly one handler, ours: any other (a library calling
    # basicConfig, say) would write unscrubbed lines.
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Exceptions nobody catches, and warnings, go through the same pipeline: Python's own
    # handlers would print the message and source lines to stderr.
    sys.excepthook = _log_uncaught_exception
    threading.excepthook = _log_uncaught_thread_exception
    sys.unraisablehook = _log_unraisable_exception
    warnings.showwarning = _log_warning
    # uvicorn installs its own handlers and levels before the app factory runs.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(level)
    # uvicorn's access line carries the client IP and the raw path with its query string;
    # AccessLogMiddleware writes ours instead. uvicorn checks for handlers per connection,
    # so the logger is switched off rather than left to propagate.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True
    # These log full URLs (query strings: API keys, OTP texts) or noise at INFO/DEBUG.
    quiet_level = max(logging.WARNING, logging.getLevelNamesMapping()[level])
    for name in ("httpx", "httpcore", "urllib3", "asyncio", "hpack"):
        logging.getLogger(name).setLevel(quiet_level)


class _CurrentStderrHandler(logging.StreamHandler[TextIO]):
    """Write to whatever `sys.stderr` is when a line is emitted, not when configured."""

    def emit(self, record: logging.LogRecord) -> None:
        """Write the line, then ship that same line (never a second, unscrubbed rendering)."""
        self.stream = sys.stderr
        try:
            line = self.format(record)
            self.stream.write(line + self.terminator)
            self.flush()
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 - logging's own contract: report, never raise
            self.handleError(record)
            return
        export = _export
        if export is None or record.name.split(".", 1)[0] in _NOT_SHIPPED:
            return
        with contextlib.suppress(Exception):  # shipping must never break logging
            export(record, line)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib name
        """One fixed line instead of logging's own dump of the traceback and arguments."""
        line = {"event": "log_emit_failed", "logger": record.name, "level": "error"}
        sys.stderr.write(json.dumps(line) + "\n")


_TRACEBACK_TEXT = "Traceback (most recent call last)"


class _StdlibRecordFilter(logging.Filter):
    """Keep exception text out of library (non-structlog) records.

    - exceptions passed as the message or a format argument become their type name;
    - a record carrying exc_info keeps only the first line of its message (asyncio, for one,
      puts the exception's repr in the lines after it);
    - a message that is itself a formatted traceback (uvicorn's lifespan failures) is
      replaced: the exception is logged separately with its type and frames.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, dict):  # structlog's own records: handled by processors
            return True
        if isinstance(record.msg, BaseException):
            record.msg = exception_type_name(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_type_if_exception(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _type_if_exception(arg) for key, arg in record.args.items()}
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format arguments: handleError reports it
            return True
        if _TRACEBACK_TEXT in message:
            message = "traceback_text_suppressed"
        elif record.exc_info:
            message = message.split("\n", 1)[0]
        else:
            return True
        record.msg, record.args = message, ()
        return True


def _type_if_exception(value: object) -> object:
    return exception_type_name(value) if isinstance(value, BaseException) else value


def _exceptions_to_types(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """structlog: an exception as the event or a %-format argument becomes its type name."""
    if isinstance(event_dict.get("event"), BaseException):
        event_dict["event"] = exception_type_name(event_dict["event"])
    args = event_dict.get("positional_args")
    if isinstance(args, tuple):
        event_dict["positional_args"] = tuple(_type_if_exception(arg) for arg in args)
    return event_dict


def _log_uncaught_exception(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    structlog.get_logger("gize_api.uncaught").error(
        "uncaught_exception", exc_info=(exc_type, exc, tb)
    )


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    structlog.get_logger("gize_api.uncaught").error(
        "uncaught_thread_exception",
        thread=args.thread.name if args.thread is not None else None,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _log_unraisable_exception(unraisable: "sys.UnraisableHookArgs") -> None:
    # err_msg and object can describe the data involved; only the exception is logged.
    structlog.get_logger("gize_api.uncaught").error(
        "unraisable_exception",
        exc_info=(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback),
    )


def _log_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    """Category and location only: like an exception message, a warning's text and source
    line can carry data, and `file:line` is enough to find it."""
    structlog.get_logger("py.warnings").warning(
        "python_warning",
        category=category.__name__,
        location=f"{_short_path(filename)}:{lineno}",
    )


# Load-balancer probes, every few seconds per instance: logged only when they fail.
QUIET_ROUTES = frozenset({"/health/live", "/health/ready"})


class AccessLogMiddleware:
    """One `request` line per HTTP request: method, route template, status, duration.

    Successful health probes (QUIET_ROUTES) are not logged; failed ones are.

    The route template (`/v1/riders/{rider_id}`) is logged, never the raw path or query.
    5xx lines are errors. The staff ID set during the request appears here, and is cleared
    before the next request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = structlog.get_logger("gize_api.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _staff_id.set(None)
        started = time.perf_counter()
        status = 500

        async def send_and_record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            if not (route in QUIET_ROUTES and status < 400):
                log = self.logger.error if status >= 500 else self.logger.info
                log(
                    "request",
                    method=known_method(scope["method"]),
                    route=route,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            _staff_id.reset(token)


def known_method(method: str) -> str:
    """The HTTP method, or `_OTHER`: clients choose it, so it must not grow logs or labels."""
    return method if method in HTTP_METHODS else "_OTHER"


def _add_request_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    request_id = current_request_id()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    staff_id = _staff_id.get()
    if staff_id is not None:
        event_dict.setdefault("staff_id", staff_id)
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict.setdefault("trace_id", format(span_context.trace_id, "032x"))
    return event_dict


def _format_exception(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Replace exc_info with the exception's type, cause chain and frames, never its text."""
    event_dict.pop("stack_info", None)
    exc = _exception_from(event_dict.pop("exc_info", None))
    if exc is None:
        return event_dict
    event_dict["exception_type"] = exception_type_name(exc)
    chain = [exc]
    while len(chain) < MAX_CHAIN:
        cause = chain[-1].__cause__ or (
            None if chain[-1].__suppress_context__ else chain[-1].__context__
        )
        if cause is None or cause in chain:
            break
        chain.append(cause)
    event_dict["exception_chain"] = [exception_type_name(item) for item in chain]
    # Root cause first, as Python prints it: where the failure started comes first.
    frames = [frame for item in reversed(chain) for frame in _frames(item.__traceback__)]
    event_dict["exception_frames"] = frames[-MAX_FRAMES:]
    return event_dict


def _exception_from(exc_info: object) -> BaseException | None:
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        value = exc_info[1]
        return value if isinstance(value, BaseException) else None
    return None


def _frames(tb: TracebackType | None) -> list[str]:
    frames = traceback.extract_tb(tb, limit=-MAX_FRAMES)
    return [f"{_short_path(frame.filename)}:{frame.lineno} in {frame.name}" for frame in frames]


def _short_path(path: str) -> str:
    """Path inside the package or library, without the machine's directories."""
    for marker in ("site-packages/", "src/", "tests/"):
        if marker in path:
            return (
                path.rsplit(marker, 1)[1]
                if marker == "site-packages/"
                else (marker + path.rsplit(marker, 1)[1])
            )
    return path.rsplit("/", 1)[-1]


def _scrub(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    try:
        budget = Budget()
        return {key: scrub_value(key, value, budget=budget) for key, value in event_dict.items()}
    except Exception:  # noqa: BLE001 - a log line must never break the request
        return {
            "event": "log_scrub_failed",
            "logger": event_dict.get("logger"),
            "level": event_dict.get("level"),
            "timestamp": event_dict.get("timestamp"),
            "request_id": event_dict.get("request_id"),
        }
