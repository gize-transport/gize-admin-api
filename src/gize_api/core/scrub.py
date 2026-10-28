"""PII and secret scrubbing for structured log fields, library log lines and Sentry events.

Exception messages are never logged at all (see core/logging.py). What remains is our own
structured fields, scrubbed by key, and free text from libraries, scrubbed by pattern:
tokens, credentials, OTP codes and secret or personal `key=value` pairs become
`[REDACTED]`; phone numbers are masked as `+2519••••1234`. Work per log event is bounded.
"""

import dataclasses
import itertools
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from pydantic import BaseModel

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
MAX_TEXT_LENGTH = 4096
MAX_DEPTH = 6
MAX_ITEMS = 50
MAX_NODES_PER_EVENT = 256
MAX_CHARS_PER_EVENT = 32_768

# Key classification works on segments: "X-Api-Key" -> (x, api, key); camelCase is split
# first: "accessToken" -> (access, token).
_SECRET_SEGMENTS = frozenset(
    [
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
    ]
)
_SECRET_LAST_SEGMENTS = frozenset({"key", "hash"})
_SECRET_KEYS = frozenset({"key", "hash"})
_PERSONAL_SEGMENTS = frozenset(
    [
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
        "home",
        "family",
        "destination",
    ]
)
_PERSONAL_KEYS = frozenset({"input", "input_value", "first", "last"})
# "<prefix>_name" keys that name things, not people.
_THING_NAME_PREFIXES = frozenset(
    [
        "table",
        "column",
        "constraint",
        "schema",
        "index",
        "route",
        "corridor",
        "event",
        "logger",
        "service",
        "file",
        "function",
        "func",
        "module",
        "metric",
        "span",
        "setting",
        "host",
        "db",
        "header",
        "field",
        "operation",
        "queue",
        "job",
        "task",
    ]
)
_PHONE_SEGMENTS = frozenset(
    {"phone", "phones", "phonenumber", "msisdn", "mobile", "telephone", "tel"}
)
_OTP_CODE_PREFIXES = frozenset(
    {"otp", "sms", "verification", "verify", "auth", "reset", "pin", "confirmation", "login"}
)
# Counters, flags and timestamps about a secret are not the secret ("otp_attempts").
_SAFE_LAST_SEGMENTS = frozenset(
    {"count", "attempts", "total", "length", "at", "ttl", "expires", "enabled", "required"}
)
# Set by us, never personal; trusted only as top-level scalars (masking would corrupt them).
TRUSTED_KEYS = frozenset(
    [
        "timestamp",
        "level",
        "logger",
        "request_id",
        "staff_id",
        "trace_id",
        "span_id",
        "event_id",
        "duration_ms",
        "exception_type",
        "exception_chain",
        "exception_frames",
    ]
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")
_SHORT_DIGITS = re.compile(r"\d{4,8}")

# Phones. Letters or digits on either side mean an identifier (hex, UUID) and are left
# alone; an escaped "\n" or "\t" still counts as a boundary. UUIDs are skipped whole.
_BOUNDARY_BEFORE = r"(?:(?<=\\[nrt])|(?<![A-Za-z0-9+]))"
_ETHIOPIAN_PHONE = re.compile(
    _BOUNDARY_BEFORE + r"(?P<prefix>(?:\+|00)?251[ .-]?(?:\(0\))?[ .-]?[79]|\(?0[ .-]?[79])"
    r"(?P<middle>(?:[ .()-]{0,2}\d){4})(?P<last>(?:[ .()-]{0,2}\d){4})(?![A-Za-z0-9])"
)
# "+" numbers may contain separators; a bare "00" prefix only counts with unbroken digits.
_INTERNATIONAL_PHONE = re.compile(
    _BOUNDARY_BEFORE + r"(?:\+(?:[ .()-]{0,2}\d){10,15}|00\d{10,15})(?![A-Za-z0-9])"
)
_UUID = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})")
_PREFIX_NOISE = re.compile(r"\(0\)|[ .-]")
_BARE_ETHIOPIAN_MOBILE = re.compile(r"[79]\d{8}")
_FULL_ETHIOPIAN_MOBILE = re.compile(r"251[79]\d{8}")

# Secrets and codes in free text. Every quantifier is bounded and text is truncated first.
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_JWT = re.compile(r"\beyJ[\w-]{1,2048}\.[\w-]{1,4096}\.[\w-]{0,1024}")
_PYDANTIC_INPUT = re.compile(r"(input_value=).{0,4096}?(, input_type=)", re.DOTALL)
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie)(\s{0,3}[=:]\s{0,3})[^\n]{1,4096}"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|digest)\s{1,4}[\w.~+/=-]{1,4096}")
_EMAIL = re.compile(
    r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}(?![\w-])"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://)[^/\s]{1,256}@")
_CODE_NUMBER = r"\d{3,4}[ -]?\d{1,4}"
# "code" alone is too generic ("route code 1203"): it needs an OTP qualifier before it, or
# ":", "=", "is" or "was" after it.
_OTP_KEYWORD_FIRST = re.compile(
    r"(?i)(\b(?:otps?|pins?|passcodes?|password|codes)\b|ኮድ"
    r"|\b(?:sms|otp|verification|security|confirmation|login|your|one-time|access)"
    r"[\W_]{1,3}code\b|\bcode(?=[\W_]{0,2}(?:[:=]|(?:is|was)\b)))"
    r"([\W_]{0,3}(?:(?:is|was)[\W_]{1,3})?)"
    rf"((?<!\d){_CODE_NUMBER}(?!\d)(?:[\W_]{{1,3}}{_CODE_NUMBER}(?!\d)){{0,5}})"
)
_OTP_NUMBER_FIRST = re.compile(
    r"(?i)(?<!\d)\d{4,8}(?!\d)(?=\s{1,3}(?:is|was)\s{1,3}(?:your|the)\b)"
)
_LIST_NUMBER = re.compile(rf"(?<!\d){_CODE_NUMBER}(?!\d)")
# `key=` / `key:` and `('key', ...)`: only the key and separator are matched, so a value is
# never consumed by a key; _key_kind decides in Python whether to redact.
_ASSIGNMENT = re.compile(r"""(?P<q>["']?)(?P<key>\b[A-Za-z][\w.-]{0,63})(?P=q)\s{0,3}[=:]\s{0,3}""")
_PAIR = re.compile(r"""\(\s{0,2}b?(?P<q>["'])(?P<key>[A-Za-z][\w.-]{0,63})(?P=q)\s{0,2},\s{0,2}""")
_BARE_VALUE_END = re.compile(r"""[,;&}\]\n"'<>)]""")
_OPENING = {"[": "]", "{": "}", "(": ")"}


class Budget:
    """Bounds the scrubbing work for one log event or Sentry event."""

    def __init__(self) -> None:
        self.nodes = MAX_NODES_PER_EVENT
        self.chars = MAX_CHARS_PER_EVENT

    def spend(self, chars: int = 0) -> bool:
        """Charge one value; False once the event has used its budget."""
        self.nodes -= 1
        self.chars -= chars
        return self.nodes >= 0 and self.chars >= 0


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers in free text, keeping the prefix and the last four digits."""
    parts = _UUID.split(text)
    for index in range(0, len(parts), 2):  # odd indexes are the UUIDs themselves
        masked = _ETHIOPIAN_PHONE.sub(_mask_ethiopian, parts[index])
        parts[index] = _INTERNATIONAL_PHONE.sub(_mask_international, masked)
    return "".join(parts)


def scrub_text(text: str) -> str:
    """Truncate, decode percent-escapes, then remove tokens, credentials, OTP codes and
    secret/personal `key=value` pairs, and mask phone numbers."""
    if len(text) > MAX_TEXT_LENGTH:
        head = text[:MAX_TEXT_LENGTH]
        cut = head.rsplit(None, 1)[0] if any(c.isspace() for c in head) else ""
        text = f"{cut}…{TRUNCATED}"  # never keep a partial token: it could be most of a secret
    if _PERCENT_ESCAPE.search(text):
        text = unquote(text)
    text = _JWT.sub(REDACTED, text)
    text = _PYDANTIC_INPUT.sub(rf"\1{REDACTED}\2", text)
    text = _AUTH_HEADER.sub(rf"\1\2{REDACTED}", text)
    text = _AUTH_SCHEME.sub(rf"\1 {REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _EMAIL.sub(REDACTED, text)
    text = _redact_values(text, _PAIR)
    text = _redact_values(text, _ASSIGNMENT)
    text = _OTP_KEYWORD_FIRST.sub(_redact_code_list, text)
    text = _OTP_NUMBER_FIRST.sub(REDACTED, text)
    return mask_phone_numbers(text)


def scrub_value(
    key: str,
    value: Any,  # noqa: ANN401 - any log value
    depth: int = 0,
    budget: Budget | None = None,
) -> Any:  # noqa: ANN401 - any log value
    """Scrub one value by its key and type, recursing into containers and objects."""
    budget = budget if budget is not None else Budget()
    kind = _key_kind(key)
    scalar = value is None or isinstance(value, (str, int, float, bool))
    if kind == "trusted" and depth == 0 and scalar:
        return value
    if not budget.spend(len(value) if isinstance(value, str) else 0):
        return TRUNCATED
    if kind in ("secret", "personal"):
        return REDACTED if value not in (None, "") else value
    if kind == "phone" and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _mask_phone_value(value)
    if kind == "code" and isinstance(value, (str, int)) and _SHORT_DIGITS.fullmatch(str(value)):
        return REDACTED  # an OTP; error codes like INTERNAL_ERROR stay readable
    if value is None or isinstance(value, (bool, float)):
        return value
    if isinstance(value, int):
        return _scrub_int(value)
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, BaseException):
        return exception_type_name(value)  # never the message: it holds the failing data
    if isinstance(value, Enum):
        return scrub_value(key, value.value, depth, budget)
    if isinstance(value, (UUID, datetime, date, time, Decimal, PurePath)):
        return scrub_text(str(value))
    if depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, (bytes, bytearray)):
        return f"[BYTES {len(value)}]"
    # Objects are walked one level at a time, never copied whole (they can be huge).
    if isinstance(value, BaseModel):
        names = list(type(value).model_fields)
        return _scrub_fields(((n, getattr(value, n)) for n in names), len(names), depth, budget)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.fields(value)
        pairs = ((f.name, getattr(value, f.name)) for f in fields)
        return _scrub_fields(pairs, len(fields), depth, budget)
    if isinstance(value, Mapping):
        return _scrub_fields(value.items(), len(value), depth, budget)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _scrub_sequence(key, value, depth, budget)
    return scrub_text(repr(value))


def exception_type_name(exc: BaseException) -> str:
    cls = type(exc)
    return cls.__name__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__name__}"


def _scrub_fields(
    pairs: Iterable[tuple[Any, Any]], total: int, depth: int, budget: Budget
) -> dict[str, Any]:
    scrubbed = {
        scrub_text(str(k)): scrub_value(str(k), v, depth + 1, budget)
        for k, v in itertools.islice(pairs, MAX_ITEMS)
    }
    if total > MAX_ITEMS:
        scrubbed["…"] = f"[+{total - MAX_ITEMS} more]"
    return scrubbed


def _scrub_sequence(key: str, value: Any, depth: int, budget: Budget) -> list[Any]:  # noqa: ANN401
    total = len(value)
    elements = list(itertools.islice(value, MAX_ITEMS))
    # A (name, value) pair such as an ASGI header: decoded, and scrubbed by its name.
    if total == 2 and isinstance(elements[0], (str, bytes)):
        name = str(_as_text(elements[0]))
        return [scrub_text(name), scrub_value(name, _as_text(elements[1]), depth + 1, budget)]
    scrubbed = [scrub_value(key, item, depth + 1, budget) for item in elements]
    if total > MAX_ITEMS:
        scrubbed.append(f"[+{total - MAX_ITEMS} more]")
    return scrubbed


def _as_text(value: Any) -> Any:  # noqa: ANN401
    return value.decode("latin-1") if isinstance(value, bytes) else value


def _scrub_int(value: int) -> int | str:
    if value.bit_length() > 14_000:  # str() refuses ints over 4300 digits
        return "[INT]"
    text = str(value)
    return mask_phone_numbers(text) if _FULL_ETHIOPIAN_MOBILE.fullmatch(text) else value


def _segments(key: str) -> list[str]:
    return [s for s in _SEGMENT_SPLIT.split(_CAMEL_BOUNDARY.sub("_", key).lower()) if s]


def _key_kind(key: str) -> str | None:
    """trusted | secret | phone | personal | code | id | None (an ordinary key)."""
    lowered = key.lower()
    if lowered in TRUSTED_KEYS:
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
    is_thing_name = segments[-1] == "name" and segments[0] in _THING_NAME_PREFIXES
    if (lowered in _PERSONAL_KEYS or _PERSONAL_SEGMENTS.intersection(segments)) and not (
        is_thing_name
    ):
        return "personal"
    if segments[-1] == "code" and (len(segments) == 1 or segments[0] in _OTP_CODE_PREFIXES):
        return "code"
    if segments[-1] == "id":
        return "id"
    return None


def _redact_values(text: str, pattern: re.Pattern[str]) -> str:
    """Replace the value after every secret, personal, phone or OTP-code key in free text."""
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    pieces: list[str] = []
    position = 0
    for index, match in enumerate(matches):
        start = match.end()
        kind = _key_kind(match["key"])
        if start <= position or kind not in ("secret", "personal", "phone", "code"):
            continue
        next_key = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        end, quote = _value_span(text, start, next_key)
        value = text[start:end]
        inner = value[len(quote) : len(value) - len(quote) if quote else None]
        if end <= start or inner.strip() in ("", REDACTED):
            continue
        replacement = _replace_value(kind, inner)
        if replacement is None:
            continue
        pieces.append(text[position:start])
        pieces.append(f"{quote}{replacement}{quote}" if quote else replacement)
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _replace_value(kind: str | None, inner: str) -> str | None:
    if kind in ("secret", "personal"):
        return REDACTED
    if kind == "phone":
        return _mask_phone_value(inner.strip())
    if kind == "code" and _SHORT_DIGITS.fullmatch(inner.strip()):
        return REDACTED
    return None


def _value_span(text: str, start: int, next_key: int) -> tuple[int, str]:
    """End of the value at `start` and its quote (with any b prefix), "" if unquoted."""
    opening = start + 1 if text.startswith(("b'", 'b"'), start) else start
    if opening >= len(text):
        return start, ""
    first = text[opening]
    if first in "\"'":
        close = _closing_quote(text, opening)
        if close is None:
            line_end = text.find("\n", opening)
            return (line_end if line_end != -1 else len(text)), ""
        return close + 1, text[start : opening + 1]
    if first in _OPENING:
        return _closing_bracket(text, opening), ""
    delimiter = _BARE_VALUE_END.search(text, start, next_key)
    end = delimiter.start() if delimiter else next_key
    while end > start and text[end - 1].isspace():
        end -= 1
    return end, ""


def _closing_quote(text: str, opening: int) -> int | None:
    quote = text[opening]
    limit = min(len(text), opening + 1 + MAX_TEXT_LENGTH)
    index = opening + 1
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return None
        if char == quote:
            return index
        index += 1
    return None


def _closing_bracket(text: str, opening: int) -> int:
    """Index just past the bracket closing the one at `opening` (quotes honoured)."""
    stack = [_OPENING[text[opening]]]
    limit = min(len(text), opening + 1 + MAX_TEXT_LENGTH)
    index = opening + 1
    while index < limit and stack:
        char = text[index]
        if char in "\"'":
            close = _closing_quote(text, index)
            index = (close if close is not None else limit) + 1
            continue
        if char in _OPENING:
            stack.append(_OPENING[char])
        elif char == stack[-1]:
            stack.pop()
        index += 1
    return index


def _redact_code_list(match: re.Match[str]) -> str:
    return match[1] + match[2] + _LIST_NUMBER.sub(REDACTED, match[3])


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
    lead = "00" if text.startswith("00") else "+"
    digits = re.sub(r"[^0-9]", "", text)
    digits = digits[2:] if lead == "00" else digits
    return f"{lead}{digits[:3]}••••{digits[-4:]}"
