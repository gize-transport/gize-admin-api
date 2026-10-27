"""Structured JSON logs (structlog) with request context and PII scrubbing.

Every line is one JSON object: timestamp (UTC), level, logger, event, request_id and, once
authenticated, staff_id. Standard-library loggers (uvicorn, libraries) go through the same
pipeline. Scrubbing runs last, after exceptions are formatted, so it covers traceback text:
secrets and personal names are replaced, phone numbers masked as `+2519••••1234`.
"""

import logging
import re
import sys
import time
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, Processor, WrappedLogger

from gize_api.core.request_id import current_request_id

REDACTED = "[REDACTED]"

# Keys whose values are secrets, matched as substrings of the lower-cased key.
_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "otp",
    "dsn",
    "signature",
)
# Keys whose values are personal data (names, children's details), matched exactly.
_PERSONAL_KEYS = frozenset(
    {
        "name",
        "full_name",
        "first_name",
        "last_name",
        "guardian_name",
        "child_name",
        "children",
        "student_name",
        "date_of_birth",
        "email",
    }
)
# Values that are ours and never personal: masking them would only corrupt them.
_TRUSTED_KEYS = frozenset({"timestamp", "level", "logger", "request_id", "staff_id", "route"})

# Ethiopian mobile numbers (+2519…, 2519…, 09…; also 7 for Safaricom), then any other
# E.164 number. The prefix and last four digits stay so support can still tell numbers apart.
_ETHIOPIAN_PHONE = re.compile(r"(?<!\d)(\+?251[79]|0[79])\d{4}(\d{4})(?!\d)")
_E164_PHONE = re.compile(r"(?<![\d+])(\+\d{3})\d{4,10}(\d{4})(?!\d)")

# Secrets inside free text (exception messages, library log lines).
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(\w*(?:password|passwd|secret|token|otp|api_?key|signature)\w*)(\s*[=:]\s*)"
    r"[^\s,;&]+"
)

_staff_id: ContextVar[str | None] = ContextVar("staff_id", default=None)


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers inside free text, keeping the prefix and the last four digits."""
    text = _ETHIOPIAN_PHONE.sub(r"\1••••\2", text)
    return _E164_PHONE.sub(r"\1••••\2", text)


def scrub_text(text: str) -> str:
    """Remove tokens and `secret=value` pairs from free text, then mask phone numbers."""
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)
    return mask_phone_numbers(text)


@contextmanager
def bind_staff_id(staff_id: str) -> Iterator[None]:
    """Attach the authenticated staff member to every log line in this context."""
    token = _staff_id.set(staff_id)
    try:
        yield
    finally:
        _staff_id.reset(token)


def log_security_event(event: str, **fields: Any) -> None:  # noqa: ANN401 - log fields
    """Failed auth, permission denials, rate-limit hits, OTP abuse: WARNING, alerted on."""
    structlog.get_logger("gize_api.security").warning(event, category="security", **fields)


def configure_logging(level: str) -> None:
    """Route structlog and the standard library through one JSON pipeline on stderr."""
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
            structlog.processors.format_exc_info,
            _scrub,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = _CurrentStderrHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers before the app factory runs; send it through ours.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # uvicorn's access line carries the client IP and the raw path with its query string;
    # AccessLogMiddleware writes ours instead. uvicorn checks for handlers per connection,
    # so the logger is switched off rather than left to propagate.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


class _CurrentStderrHandler(logging.StreamHandler[TextIO]):
    """Write to whatever `sys.stderr` is when a line is emitted, not when configured."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


class AccessLogMiddleware:
    """One `request` line per HTTP request: method, route template, status, duration.

    The route template (`/v1/riders/{rider_id}`) is logged, never the raw path or query.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = structlog.get_logger("gize_api.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
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
            self.logger.info(
                "request",
                method=scope["method"],
                route=route,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )


def _add_request_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    request_id = current_request_id()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    staff_id = _staff_id.get()
    if staff_id is not None:
        event_dict.setdefault("staff_id", staff_id)
    return event_dict


def _scrub(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        event_dict[key] = scrub_value(key, event_dict[key])
    return event_dict


def scrub_value(key: str, value: Any) -> Any:  # noqa: ANN401 - any JSON-like log value
    """Scrub one value by its key, recursing into dicts and lists."""
    lowered = key.lower()
    if lowered in _TRUSTED_KEYS:
        return value
    if any(part in lowered for part in _SECRET_KEY_PARTS) or lowered in _PERSONAL_KEYS:
        return REDACTED if value not in (None, "") else value
    if "phone" in lowered and isinstance(value, str):
        masked = mask_phone_numbers(value)
        return masked if masked != value else REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, MutableMapping):
        return {k: scrub_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(key, item) for item in value]
    return value
