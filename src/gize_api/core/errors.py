"""RFC 9457 problem details (`application/problem+json`) for every error response.

Clients get `type`, `title`, `status`, a stable machine `code` and the `request_id`.
Validation errors add `errors` (location, field, reason) and `errors_truncated`. Never
exception text, SQL, stack traces, input values or internal IDs: those go to the logs with
the same request ID. Codes and reasons are part of the API contract: only ever add.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Literal

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gize_api.core.db import (
    database_sqlstate,
    is_database_misconfigured,
    is_database_unavailable,
)
from gize_api.core.request_id import current_request_id
from gize_api.core.sentry import report_exception

PROBLEM_JSON = "application/problem+json"
MAX_VALIDATION_ERRORS = 50
MAX_FIELD_PART_LENGTH = 64
MAX_FIELD_PARTS = 8

_LOGGED_FLAG = "gize_unhandled_error_logged"

logger = structlog.get_logger("gize_api.errors")


class ErrorCode(StrEnum):
    """Stable error codes: never rename or reuse one; only add."""

    BAD_REQUEST = "BAD_REQUEST"
    CORS_PREFLIGHT_REJECTED = "CORS_PREFLIGHT_REJECTED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_CURSOR = "INVALID_CURSOR"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    RATE_LIMITED = "RATE_LIMITED"
    STAFF_EMAIL_TAKEN = "STAFF_EMAIL_TAKEN"
    AUTH_ACCOUNT_EXISTS = "AUTH_ACCOUNT_EXISTS"
    LAST_ACTIVE_ADMIN = "LAST_ACTIVE_ADMIN"
    AREA_NAME_TAKEN = "AREA_NAME_TAKEN"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class ValidationReason(StrEnum):
    """Why a field failed validation: our vocabulary, not the validation library's."""

    MISSING = "missing"
    UNKNOWN_FIELD = "unknown_field"
    TOO_LONG = "too_long"
    TOO_SHORT = "too_short"
    OUT_OF_RANGE = "out_of_range"
    INVALID_TYPE = "invalid_type"
    INVALID_FORMAT = "invalid_format"
    NOT_ALLOWED = "not_allowed"
    INVALID_JSON = "invalid_json"
    INVALID = "invalid"


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    title: str


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.BAD_REQUEST: ErrorSpec(400, "Bad Request"),
    ErrorCode.CORS_PREFLIGHT_REJECTED: ErrorSpec(400, "CORS Preflight Rejected"),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, "Unauthenticated"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "Forbidden"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, "Not Found"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "Method Not Allowed"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(413, "Payload Too Large"),
    ErrorCode.VALIDATION_FAILED: ErrorSpec(422, "Validation Failed"),
    ErrorCode.INVALID_CURSOR: ErrorSpec(422, "Invalid Cursor"),
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: ErrorSpec(428, "Idempotency Key Required"),
    ErrorCode.IDEMPOTENCY_KEY_MISMATCH: ErrorSpec(422, "Idempotency Key Mismatch"),
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: ErrorSpec(409, "Request In Progress"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "Too Many Requests"),
    ErrorCode.STAFF_EMAIL_TAKEN: ErrorSpec(409, "Email Already Used By Staff"),
    ErrorCode.AUTH_ACCOUNT_EXISTS: ErrorSpec(409, "Sign-In Account Already Exists"),
    ErrorCode.LAST_ACTIVE_ADMIN: ErrorSpec(409, "Last Active Admin"),
    ErrorCode.AREA_NAME_TAKEN: ErrorSpec(409, "Area Name Taken"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "Internal Server Error"),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "Service Unavailable"),
}

# Framework HTTP errors (routing, method, an HTTPException raised by code) mapped onto codes.
_CODE_FOR_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}

Location = Literal["body", "query", "path", "header", "cookie"]
_LOCATIONS: dict[str, Location] = {
    "body": "body",
    "query": "query",
    "path": "path",
    "header": "header",
    "cookie": "cookie",
}


class FieldError(BaseModel):
    location: Location
    # JSON Pointer (RFC 6901) within `location`; "" means the whole body or parameter set.
    field: str
    reason: ValidationReason


class Problem(BaseModel):
    # Always sent, so the contract lists `type` as required although it has a default.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    type: str = "about:blank"
    title: str
    status: int
    code: ErrorCode
    request_id: str | None


class ValidationProblem(Problem):
    errors: list[FieldError]
    errors_truncated: bool


class AppError(Exception):
    """Raise from services to return a problem response with a registered code."""

    def __init__(self, code: ErrorCode, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.headers = headers


def problem_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """The error responses an operation documents, for a route's `responses=`.

    Codes are grouped by status; core/contract.py adds the problem+json schema. A route's
    entry for a status replaces its router's (FastAPI merges per status), so list every
    code for that status in one place. 422 VALIDATION_FAILED, 413 and 500 are added for
    every operation that can return them.
    """
    if not codes:
        raise ValueError("problem_responses needs at least one error code")
    by_status: dict[int, set[ErrorCode]] = {}
    for code in codes:
        by_status.setdefault(ERROR_SPECS[code].status, set()).add(code)
    return {
        status: {
            "description": ", ".join(sorted({ERROR_SPECS[code].title for code in group})),
            "x-error-codes": sorted(code.value for code in group),
        }
        for status, group in sorted(by_status.items())
    }


def problem_response(
    code: ErrorCode,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
    errors: Sequence[FieldError] | None = None,
    errors_truncated: bool = False,
) -> JSONResponse:
    """The problem response for `code`. `status` overrides the registered one (framework)."""
    spec = ERROR_SPECS[code]
    if status is None or status == spec.status:
        status, title = spec.status, spec.title
    else:
        title = _phrase(status, fallback=spec.title)
    problem: Problem
    if errors is None:
        problem = Problem(title=title, status=status, code=code, request_id=current_request_id())
    else:
        problem = ValidationProblem(
            title=title,
            status=status,
            code=code,
            request_id=current_request_id(),
            errors=list(errors),
            errors_truncated=errors_truncated,
        )
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


def reason_for(pydantic_type: str) -> ValidationReason:
    """Map a pydantic error type onto our stable reason vocabulary."""
    exact = {
        "missing": ValidationReason.MISSING,
        "extra_forbidden": ValidationReason.UNKNOWN_FIELD,
        "json_invalid": ValidationReason.INVALID_JSON,
        "literal_error": ValidationReason.NOT_ALLOWED,
        "enum": ValidationReason.NOT_ALLOWED,
        "greater_than": ValidationReason.OUT_OF_RANGE,
        "greater_than_equal": ValidationReason.OUT_OF_RANGE,
        "less_than": ValidationReason.OUT_OF_RANGE,
        "less_than_equal": ValidationReason.OUT_OF_RANGE,
        "multiple_of": ValidationReason.OUT_OF_RANGE,
        "finite_number": ValidationReason.OUT_OF_RANGE,
    }
    if pydantic_type in exact:
        return exact[pydantic_type]
    if pydantic_type.endswith("too_long"):
        return ValidationReason.TOO_LONG
    if pydantic_type.endswith("too_short"):
        return ValidationReason.TOO_SHORT
    if pydantic_type.startswith(_FORMAT_PREFIXES):
        return ValidationReason.INVALID_FORMAT
    if pydantic_type.endswith(("_type", "_parsing")):
        return ValidationReason.INVALID_TYPE
    return ValidationReason.INVALID


_FORMAT_PREFIXES = ("string_pattern", "uuid", "url", "date", "time", "ip_", "email", "decimal")


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    for database_error in (SQLAlchemyError, OSError, TimeoutError):
        app.add_exception_handler(database_error, _database_error)
    app.add_exception_handler(Exception, _unhandled_error)


DATABASE_RETRY_AFTER_SECONDS = "5"
# A misconfigured database fails every request; report it once per interval, not each time.
MISCONFIGURATION_REPORT_INTERVAL_SECONDS = 60.0
_last_misconfiguration_report: dict[tuple[str, str | None], float] = {}


async def _database_error(request: Request, exc: Exception) -> Response:
    """A database that is down, slow, saturated or asking for a retry is 503; any other
    error stays a 500. The SQLSTATE (a code, never message text) says which."""
    if not is_database_unavailable(exc):
        return await _unhandled_error(request, exc)
    route = _route(request.scope)
    misconfigured = is_database_misconfigured(exc)
    logger.error(
        "database_unavailable",
        route=route,
        sqlstate=database_sqlstate(exc),
        misconfigured=misconfigured,
        exc_info=exc,
    )
    if misconfigured and _due_for_report(type(exc).__name__, database_sqlstate(exc)):
        # An outage passes; a wrong password or certificate needs a human.
        report_exception(exc, request_id=current_request_id(), route=route)
    telemetry = getattr(request.app, "telemetry", None)
    if telemetry is not None:
        telemetry.record_database_unavailable(exc)
    return problem_response(
        ErrorCode.SERVICE_UNAVAILABLE, headers={"Retry-After": DATABASE_RETRY_AFTER_SECONDS}
    )


def _due_for_report(error_type: str, sqlstate: str | None) -> bool:
    key, now = (error_type, sqlstate), time.monotonic()
    last = _last_misconfiguration_report.get(key)
    if last is not None and now - last < MISCONFIGURATION_REPORT_INTERVAL_SECONDS:
        return False
    _last_misconfiguration_report[key] = now
    return True


class LastResortGuard:
    """Catch an error escaping our own middleware (a bug), just inside the tracing layer.

    It is logged as type and frames, answered with a generic 500 if nothing was sent yet,
    and never re-raised: otherwise the server, and the tracing layer around it, would record
    the exception's message and stack text.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error("unhandled_error_outside_handlers", exc_info=exc)
            if not started:
                await problem_response(ErrorCode.INTERNAL_ERROR)(scope, receive, send)


class HandledErrorGuard:
    """Stop an error our handler already logged from reaching the server.

    Starlette's ServerErrorMiddleware calls the handler (which logs with the request ID and,
    unless the response already started, sends the 500) and then re-raises, so the server
    would log it a second time: without a request ID and with the raw exception text. An
    error the handler did not see is re-raised, never lost. If the response had started,
    returning leaves it incomplete and the server aborts the connection, so a client never
    mistakes a truncated body for success. Installed directly around ServerErrorMiddleware
    (see main.GizeAPI).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception:
            if not scope.get("state", {}).get(_LOGGED_FLAG):
                raise


# Starlette types every handler as (Request, Exception); each re-raises anything it was not
# registered for, which also narrows the type without `assert`.


async def _app_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):
        raise exc
    return problem_response(exc.code, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        raise exc
    all_errors = exc.errors()
    errors = [_field_error(error["loc"], str(error["type"])) for error in all_errors]
    return problem_response(
        ErrorCode.VALIDATION_FAILED,
        errors=errors[:MAX_VALIDATION_ERRORS],
        errors_truncated=len(all_errors) > MAX_VALIDATION_ERRORS,
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    if exc.status_code < 400:
        return Response(status_code=exc.status_code, headers=exc.headers)
    if exc.status_code >= 500:
        logger.error("http_error", status=exc.status_code, route=_route(request.scope))
    code = _CODE_FOR_STATUS.get(exc.status_code)
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.BAD_REQUEST
    return problem_response(code, status=exc.status_code, headers=exc.headers)


async def _unhandled_error(request: Request, exc: Exception) -> Response:
    route = _route(request.scope)
    logger.error("unhandled_error", route=route, exc_info=exc)
    report_exception(exc, request_id=current_request_id(), route=route)
    request.scope.setdefault("state", {})[_LOGGED_FLAG] = True
    return problem_response(ErrorCode.INTERNAL_ERROR)


def _route(scope: Scope) -> str:
    """The route template (never the raw path, which can carry IDs or phone numbers)."""
    route = scope.get("route")
    return str(getattr(route, "path", "unmatched"))


def _field_error(loc: Sequence[int | str], pydantic_type: str) -> FieldError:
    location = _LOCATIONS.get(str(loc[0]), "body") if loc else "body"
    parts = [] if pydantic_type == "json_invalid" else list(loc[1:])
    pointer = "".join(
        "/" + str(part)[:MAX_FIELD_PART_LENGTH].replace("~", "~0").replace("/", "~1")
        for part in parts[:MAX_FIELD_PARTS]
    )
    return FieldError(location=location, field=pointer, reason=reason_for(pydantic_type))


def _phrase(status: int, *, fallback: str) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return fallback
