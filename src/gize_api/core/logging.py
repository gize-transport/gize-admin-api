"""Structured JSON logs (structlog) with request context and PII scrubbing.

Every line is one JSON object: timestamp (UTC), level, logger, event, request_id, trace_id
(inside a span) and, once authenticated, staff_id. Standard-library loggers (uvicorn,
libraries, warnings) go through the same pipeline. Scrubbing runs last, after exceptions
are formatted, over every value of every type: secrets and personal data are replaced by
`[REDACTED]`, phone numbers are masked as `+2519••••1234`, text is capped at 4 KB.
"""

import dataclasses
import logging
import re
import sys
import time
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, TextIO

import sentry_sdk
import structlog
from opentelemetry import trace
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, Processor, WrappedLogger

from gize_api.core.request_id import current_request_id

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
MAX_TEXT_LENGTH = 4096
MAX_DEPTH = 6

# Key classification works on segments: "X-Api-Key" -> ("x", "api", "key").
_SECRET_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "jwt",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "session",
        "otp",
        "pin",
        "dsn",
        "signature",
        "credential",
        "credentials",
        "apikey",
    }
)
_SECRET_LAST_SEGMENTS = frozenset({"key", "code", "hash"})
_PERSONAL_SEGMENTS = frozenset(
    {
        "name",
        "names",
        "email",
        "dob",
        "birth",
        "birthday",
        "address",
        "school",
        "grade",
        "guardian",
        "child",
        "children",
        "student",
        "parent",
        "passport",
    }
)
_PERSONAL_KEYS = frozenset({"national_id", "kebele_id", "date_of_birth"})
_PHONE_SEGMENTS = frozenset({"phone", "phones", "msisdn", "mobile"})
# Counters, flags and timestamps about a secret are not the secret ("otp_attempts").
_SAFE_LAST_SEGMENTS = frozenset(
    {"count", "attempts", "total", "length", "at", "ttl", "expires", "enabled", "required"}
)
# Ours and never personal: masking them would only corrupt them.
_TRUSTED_KEYS = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "request_id",
        "staff_id",
        "trace_id",
        "span_id",
        "event_id",
        "route",
        "method",
        "status",
        "duration_ms",
        "category",
    }
)
_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")

# Ethiopian mobiles (+2519…, 2519…, 09…; 7 for Safaricom), digits optionally grouped by
# spaces or dashes; then any other E.164 number. Word boundaries keep IDs (hex, UUIDs) intact.
_ETHIOPIAN_PHONE = re.compile(
    r"(?<![\w+])(?P<prefix>\+?251[ -]?[79]|0[79])(?P<middle>(?:[ -]?\d){4})"
    r"(?P<last>(?:[ -]?\d){4})(?![\w])"
)
_E164_PHONE = re.compile(r"(?<![\w+])\+(?:[ -]?\d){10,15}(?![\w])")
_BARE_ETHIOPIAN_MOBILE = re.compile(r"[79]\d{8}")

# Secrets inside free text (exception messages, reprs, library log lines). Every quantifier
# is bounded and text is truncated first, so the cost per value is linear and small.
_JWT = re.compile(r"\beyJ[\w-]{1,2048}\.[\w-]{1,4096}\.[\w-]{0,1024}")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|digest)\s{1,4}[\w.~+/=-]{1,4096}")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)[^/\s@]{1,256}@")
# A match may only start on a key containing a secret or personal word (confirmed by
# _key_kind), so ordinary "word: value" text cannot swallow the secret key after it.
_KEY_WORDS = (
    "password|passwd|pwd|secret|token|jwt|authorization|auth|cookie|session|otp|pin|dsn"
    "|signature|credential|api_?key|key|code|hash|name|email|dob|birth|address|school"
    "|grade|guardian|child|student|parent|passport|national"
)
_KEY_VALUE = re.compile(
    rf"""(?P<q1>["']?)(?P<key>\b[\w.-]{{0,30}}(?:{_KEY_WORDS})[\w.-]{{0,30}})(?P=q1)"""
    r"""(?P<sep>\s{0,3}[=:]\s{0,3})(?P<q2>["']?)(?P<value>[^\s"',;&}\]]{1,512})(?P=q2)""",
    re.IGNORECASE,
)

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
    sentry_sdk.capture_message(
        event,
        level="warning",
        tags={"category": "security", "request_id": current_request_id() or "none"},
        extras={key: scrub_value(key, value) for key, value in fields.items()},
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
            structlog.processors.format_exc_info,
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
                method=scope["method"],
                route=getattr(scope.get("route"), "path", "unmatched"),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            _staff_id.reset(token)


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers inside free text, keeping the prefix and the last four digits."""
    text = _ETHIOPIAN_PHONE.sub(_mask_ethiopian, text)
    return _E164_PHONE.sub(_mask_e164, text)


def scrub_text(text: str) -> str:
    """Truncate, remove tokens, credentials and `secret=value` pairs, mask phone numbers."""
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH] + "…" + TRUNCATED
    text = _JWT.sub(REDACTED, text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _KEY_VALUE.sub(_redact_secret_value, text)
    return mask_phone_numbers(text)


def scrub_value(key: str, value: Any, depth: int = 0) -> Any:  # noqa: ANN401 - any log value
    """Scrub one value by its key and type, recursing into containers and objects."""
    kind = _key_kind(key)
    if kind == "trusted":
        return value
    if kind in ("secret", "personal"):
        return REDACTED if value not in (None, "") else value
    if kind == "phone" and isinstance(value, (str, int)):
        return _mask_phone_value(str(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, (bytes, bytearray)):
        return f"[BYTES {len(value)}]"
    if isinstance(value, BaseModel):
        return scrub_value(key, value.model_dump(mode="json"), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return scrub_value(key, dataclasses.asdict(value), depth + 1)
    if isinstance(value, Mapping):
        return {str(k): scrub_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub_value(key, item, depth + 1) for item in value]
    return scrub_text(repr(value))


def _key_kind(key: str) -> str | None:
    lowered = key.lower()
    if lowered in _TRUSTED_KEYS:
        return "trusted"
    segments = [s for s in _SEGMENT_SPLIT.split(lowered) if s]
    if not segments or segments[-1] in _SAFE_LAST_SEGMENTS:
        return None
    if (
        _SECRET_SEGMENTS.intersection(segments)
        or (len(segments) > 1 and segments[-1] in _SECRET_LAST_SEGMENTS)
        or lowered == "key"
    ):
        return "secret"
    # A phone is masked (last four digits kept) even when it belongs to a guardian or child.
    if _PHONE_SEGMENTS.intersection(segments):
        return "phone"
    if lowered in _PERSONAL_KEYS or _PERSONAL_SEGMENTS.intersection(segments):
        return "personal"
    if segments[-1] == "id":
        return "trusted"
    return None


def _mask_phone_value(value: str) -> str:
    if "••••" in value:
        return value
    masked = mask_phone_numbers(value)
    if masked != value:
        return masked
    if _BARE_ETHIOPIAN_MOBILE.fullmatch(value):
        return f"{value[0]}••••{value[-4:]}"
    return REDACTED


def _mask_ethiopian(match: re.Match[str]) -> str:
    prefix = re.sub(r"[ -]", "", match["prefix"])
    return f"{prefix}••••{re.sub(r'[^0-9]', '', match['last'])}"


def _mask_e164(match: re.Match[str]) -> str:
    digits = re.sub(r"[^0-9]", "", match.group())
    return f"+{digits[:3]}••••{digits[-4:]}"


def _redact_secret_value(match: re.Match[str]) -> str:
    if _key_kind(match["key"]) not in ("secret", "personal"):
        return match.group()
    q1, q2 = match["q1"], match["q2"]
    return f"{q1}{match['key']}{q1}{match['sep']}{q2}{REDACTED}{q2}"


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


def _scrub(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    try:
        return {key: scrub_value(key, value) for key, value in event_dict.items()}
    except Exception:  # noqa: BLE001 - a log line must never break the request
        return {
            "event": "log_scrub_failed",
            "logger": event_dict.get("logger"),
            "level": event_dict.get("level"),
            "timestamp": event_dict.get("timestamp"),
            "request_id": event_dict.get("request_id"),
        }
