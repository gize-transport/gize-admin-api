"""Structured JSON logs (structlog) with request context and PII scrubbing.

Every line is one JSON object: timestamp (UTC), level, logger, event, request_id, trace_id
(inside a span) and, once authenticated, staff_id. Standard-library loggers (uvicorn,
libraries, warnings) go through the same pipeline. Scrubbing runs last, after exceptions
over every value of every type (core/scrub.py): secrets and personal data are replaced by
`[REDACTED]`, phone numbers are masked as `+2519••••1234`.

Exceptions are logged as `exception_type`, `exception_chain` and `exception_frames`
(`file:line in function`), never their message or source lines: messages carry whatever
data the failing code held (names, phones, codes), which no pattern can reliably catch.
"""

import logging
import sys
import time
import traceback
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
    "known_method",
    "log_security_event",
    "mask_phone_numbers",
    "scrub_text",
    "scrub_value",
    "set_staff_id",
]

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
MAX_FRAMES = 30
MAX_CHAIN = 5

_staff_id: ContextVar[str | None] = ContextVar("staff_id", default=None)


def set_staff_id(staff_id: str | None) -> None:
    """Attach the authenticated staff member to the rest of this request's log lines.

    Call from an async dependency (it shares the request's context); AccessLogMiddleware
    clears it for every new request.
    """
    _staff_id.set(staff_id)


def log_security_event(event: str, **fields: Any) -> None:  # noqa: ANN401 - log fields
    """Failed auth, permission denials, rate-limit hits, OTP abuse: logged at WARNING and
    sent to Sentry as a warning event (the alert route until logs are shipped, GZ-066)."""
    structlog.get_logger("gize_api.security").warning(event, category="security", **fields)
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
    handler.setFormatter(formatter)

    # The root logger gets exactly one handler, ours: any other (a library calling
    # basicConfig, say) would write unscrubbed lines.
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # captureWarnings(True) is a no-op if it thinks it already ran, even when something has
    # since restored the original hook; resetting first makes reconfiguration reliable.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
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
        self.stream = sys.stderr
        super().emit(record)


class AccessLogMiddleware:
    """One `request` line per HTTP request: method, route template, status, duration.

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
            log = self.logger.error if status >= 500 else self.logger.info
            log(
                "request",
                method=known_method(scope["method"]),
                route=getattr(scope.get("route"), "path", "unmatched"),
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
    event_dict["exception_type"] = _type_name(exc)
    chain = [exc]
    while len(chain) < MAX_CHAIN:
        cause = chain[-1].__cause__ or (
            None if chain[-1].__suppress_context__ else chain[-1].__context__
        )
        if cause is None or cause in chain:
            break
        chain.append(cause)
    event_dict["exception_chain"] = [_type_name(item) for item in chain]
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


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    return cls.__name__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__name__}"


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
