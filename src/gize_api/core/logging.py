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
MAX_EXCEPTION_LENGTH = 8192  # tracebacks keep their tail: the cause is at the end
MAX_DEPTH = 6
MAX_ITEMS = 50

# Key classification works on segments: "X-Api-Key" -> (x, api, key); camelCase is split
# first: "accessToken" -> (access, token).
_SECRET_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pw",
        "pass",
        "passcode",
        "passphrase",
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
_SECRET_LAST_SEGMENTS = frozenset({"key", "hash"})
_SECRET_KEYS = frozenset({"key", "hash"})
_PERSONAL_SEGMENTS = frozenset(
    {
        "name",
        "names",
        "fullname",
        "firstname",
        "lastname",
        "surname",
        "username",
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
        "national",
        "kebele",
    }
)
_PHONE_SEGMENTS = frozenset(
    {"phone", "phones", "phonenumber", "msisdn", "mobile", "telephone", "tel"}
)
# Counters, flags and timestamps about a secret are not the secret ("otp_attempts").
_SAFE_LAST_SEGMENTS = frozenset(
    {"count", "attempts", "total", "length", "at", "ttl", "expires", "enabled", "required"}
)
# Set by us, never personal; trusted only as top-level scalars (masking would corrupt them).
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
        "duration_ms",
    }
)
HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")
_SHORT_DIGITS = re.compile(r"\d{4,8}")

# Ethiopian mobiles (+2519…, 002519…, 2519…, 09…; 7 for Safaricom) with optional separators;
# then any other international number. Letters or digits on either side mean an identifier
# (hex, UUID), which is left intact.
_ETHIOPIAN_PHONE = re.compile(
    r"(?<![A-Za-z0-9+])(?P<prefix>(?:\+|00)?251[ .-]?(?:\(0\))?[ .-]?[79]|\(?0[79])"
    r"(?P<middle>(?:[ .()-]{0,2}\d){4})(?P<last>(?:[ .()-]{0,2}\d){4})(?![A-Za-z0-9])"
)
# "+" numbers may contain separators; a bare "00" prefix only counts with unbroken digits,
# or UUID groups like "0011-2233-4450" would match.
_INTERNATIONAL_PHONE = re.compile(
    r"(?<![A-Za-z0-9+])(?:\+(?:[ .()-]{0,2}\d){10,15}|00\d{10,15})(?![A-Za-z0-9])"
)
_UUID = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})")
_PREFIX_NOISE = re.compile(r"\(0\)|[ .-]")
_BARE_ETHIOPIAN_MOBILE = re.compile(r"[79]\d{8}")
_FULL_ETHIOPIAN_MOBILE = re.compile(r"251[79]\d{8}")

# Secrets inside free text (exception messages, reprs, library log lines). Every quantifier
# is bounded and text is truncated first, so the cost per value is linear and small.
_JWT = re.compile(r"\beyJ[\w-]{1,2048}\.[\w-]{1,4096}\.[\w-]{0,1024}")
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie)(\s{0,3}[=:]\s{0,3})[^\n]{1,4096}"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|digest)\s{1,4}[\w.~+/=-]{1,4096}")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)[^/\s]{1,256}@")
# pydantic validation errors echo the client's input verbatim.
_PYDANTIC_INPUT = re.compile(r"(input_value=).{0,4096}?(, input_type=)", re.DOTALL)
_OTP_IN_TEXT = re.compile(r"(?i)\b(otp|code|pin|passcode)\b(\W{0,3}(?:is\s{1,3})?)\d{4,8}\b")
# `key=` / `key:` where the key contains a secret or personal word (confirmed by _key_kind).
# Only the key and separator are matched, so values are never consumed by a non-secret key.
_KEY_WORDS = (
    "password|passwd|pwd|pw|pass|secret|token|jwt|authorization|auth|cookie|session|otp|pin"
    "|dsn|signature|credential|api_?key|key|code|hash|name|email|dob|birth|address|school"
    "|grade|guardian|child|student|parent|passport|national|kebele"
)
_KEY_SEPARATOR = re.compile(
    rf"""(?P<q>["']?)(?P<key>\b[\w.-]{{0,30}}(?:{_KEY_WORDS})[\w.-]{{0,30}})(?P=q)\s{{0,3}}[=:]\s{{0,3}}""",
    re.IGNORECASE,
)
_BARE_VALUE_END = re.compile(r"""[,;&}\]\n"'<>]""")

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
                method=known_method(scope["method"]),
                route=getattr(scope.get("route"), "path", "unmatched"),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            _staff_id.reset(token)


def known_method(method: str) -> str:
    """The HTTP method, or `_OTHER`: clients choose it, so it must not grow logs or labels."""
    return method if method in HTTP_METHODS else "_OTHER"


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers inside free text, keeping the prefix and the last four digits.

    UUIDs are skipped whole: their all-digit groups can look like numbers.
    """
    parts = _UUID.split(text)
    for index in range(0, len(parts), 2):  # odd indexes are the UUIDs themselves
        masked = _ETHIOPIAN_PHONE.sub(_mask_ethiopian, parts[index])
        parts[index] = _INTERNATIONAL_PHONE.sub(_mask_international, masked)
    return "".join(parts)


def scrub_text(text: str, *, keep_tail: bool = False) -> str:
    """Truncate, remove tokens, credentials, OTPs and secret/personal `key=value` pairs, and
    mask phone numbers. `keep_tail` keeps the end (tracebacks) instead of the start."""
    limit = MAX_EXCEPTION_LENGTH if keep_tail else MAX_TEXT_LENGTH
    if len(text) > limit:
        text = f"{TRUNCATED}…{text[-limit:]}" if keep_tail else f"{text[:limit]}…{TRUNCATED}"
    text = _JWT.sub(REDACTED, text)
    text = _PYDANTIC_INPUT.sub(rf"\1{REDACTED}\2", text)
    text = _AUTH_HEADER.sub(rf"\1\2{REDACTED}", text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _redact_assignments(text)
    text = _OTP_IN_TEXT.sub(rf"\1\2{REDACTED}", text)
    return mask_phone_numbers(text)


def scrub_value(key: str, value: Any, depth: int = 0) -> Any:  # noqa: ANN401 - any log value
    """Scrub one value by its key and type, recursing into containers and objects."""
    kind = _key_kind(key)
    scalar = value is None or isinstance(value, (str, int, float, bool))
    if kind == "trusted" and depth == 0 and scalar:
        return value
    if kind in ("secret", "personal"):
        return REDACTED if value not in (None, "") else value
    if kind == "phone" and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _mask_phone_value(value)
    if kind == "code" and _SHORT_DIGITS.fullmatch(str(value)):
        return REDACTED  # an OTP; error codes like INTERNAL_ERROR stay readable
    if value is None or isinstance(value, (bool, float)):
        return value
    if isinstance(value, int):
        text = str(value)
        return mask_phone_numbers(text) if _FULL_ETHIOPIAN_MOBILE.fullmatch(text) else value
    if isinstance(value, str):
        return scrub_text(value, keep_tail=key == "exception")
    if depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, (bytes, bytearray)):
        return f"[BYTES {len(value)}]"
    if isinstance(value, BaseModel):
        return scrub_value(key, value.model_dump(mode="json"), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return scrub_value(key, dataclasses.asdict(value), depth + 1)
    if isinstance(value, Mapping):
        items = list(value.items())
        scrubbed = {
            scrub_text(str(k)): scrub_value(str(k), v, depth + 1) for k, v in items[:MAX_ITEMS]
        }
        if len(items) > MAX_ITEMS:
            scrubbed["…"] = f"[+{len(items) - MAX_ITEMS} more]"
        return scrubbed
    if isinstance(value, (list, tuple, set, frozenset)):
        elements = list(value)
        scrubbed_list = [scrub_value(key, item, depth + 1) for item in elements[:MAX_ITEMS]]
        if len(elements) > MAX_ITEMS:
            scrubbed_list.append(f"[+{len(elements) - MAX_ITEMS} more]")
        return scrubbed_list
    return scrub_text(repr(value))


def _segments(key: str) -> list[str]:
    return [s for s in _SEGMENT_SPLIT.split(_CAMEL_BOUNDARY.sub("_", key).lower()) if s]


def _key_kind(key: str) -> str | None:
    """trusted | secret | phone | personal | code | id | None (an ordinary key)."""
    lowered = key.lower()
    if lowered in _TRUSTED_KEYS:
        return "trusted"
    segments = _segments(key)
    if not segments or segments[-1] in _SAFE_LAST_SEGMENTS:
        return None
    if (
        _SECRET_SEGMENTS.intersection(segments)
        or (len(segments) > 1 and segments[-1] in _SECRET_LAST_SEGMENTS)
        or lowered in _SECRET_KEYS
    ):
        return "secret"
    # A phone is masked (last four digits kept) even when it belongs to a guardian or child.
    if _PHONE_SEGMENTS.intersection(segments):
        return "phone"
    if _PERSONAL_SEGMENTS.intersection(segments):
        return "personal"
    if segments[-1] == "code":
        return "code"
    if segments[-1] == "id":
        return "id"
    return None


def _redact_assignments(text: str) -> str:
    """Replace the value after every secret or personal `key=` / `key:` in free text."""
    matches = list(_KEY_SEPARATOR.finditer(text))
    if not matches:
        return text
    pieces: list[str] = []
    position = 0
    for index, match in enumerate(matches):
        start = match.end()
        if start <= position:
            continue
        next_key = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        end, quoted = _value_span(text, start, next_key)
        if end <= start:
            continue
        kind = _key_kind(match["key"])
        value = text[start:end]
        inner = value.strip("b'\"") if quoted else value
        if kind in ("secret", "personal") or (kind == "code" and _SHORT_DIGITS.fullmatch(inner)):
            pieces.append(text[position:start])
            pieces.append(_requote(value, quoted))
            position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _value_span(text: str, start: int, next_key: int) -> tuple[int, bool]:
    """End of the value starting at `start`, and whether it is quoted."""
    opening = start + 1 if text.startswith(("b'", 'b"'), start) else start
    if opening < len(text) and text[opening] in "\"'":
        close = text.find(text[opening], opening + 1, opening + 1 + MAX_EXCEPTION_LENGTH)
        line_end = text.find("\n", opening + 1)
        if close == -1 or (line_end != -1 and line_end < close):
            close = line_end if line_end != -1 else len(text)
            return close, False
        return close + 1, True
    delimiter = _BARE_VALUE_END.search(text, start, next_key)
    end = delimiter.start() if delimiter else next_key
    while end > start and text[end - 1].isspace():
        end -= 1
    return end, False


def _requote(value: str, quoted: bool) -> str:
    if not quoted:
        return REDACTED
    prefix = "b" if value.startswith("b") else ""
    quote = value[len(prefix)]
    return f"{prefix}{quote}{REDACTED}{quote}"


def _mask_phone_value(value: str | int | float) -> str:
    if isinstance(value, float):
        if not value.is_integer():
            return REDACTED
        value = int(value)
    text = str(value)
    if "••••" in text:
        return text
    masked = mask_phone_numbers(text)
    if masked != text:
        return masked
    if _BARE_ETHIOPIAN_MOBILE.fullmatch(text):
        return f"{text[0]}••••{text[-4:]}"
    return REDACTED


def _mask_ethiopian(match: re.Match[str]) -> str:
    prefix = _PREFIX_NOISE.sub("", match["prefix"])
    return f"{prefix}••••{re.sub(r'[^0-9]', '', match['last'])}"


def _mask_international(match: re.Match[str]) -> str:
    text = match.group()
    digits = re.sub(r"[^0-9]", "", text)
    lead = "00" if text.startswith("00") else "+"
    digits = digits[2:] if lead == "00" else digits
    return f"{lead}{digits[:3]}••••{digits[-4:]}"


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
