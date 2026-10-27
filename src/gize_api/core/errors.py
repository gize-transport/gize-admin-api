"""RFC 9457 problem details (`application/problem+json`) for every error response.

Clients get `type`, `title`, `status`, a stable machine `code` and the `request_id`;
validation errors add `errors` (field and reason only). Never exception text, SQL, stack
traces, input values or internal IDs: those go to the logs with the same request ID.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from gize_api.core.request_id import current_request_id

PROBLEM_JSON = "application/problem+json"
MAX_VALIDATION_ERRORS = 50

logger = logging.getLogger("gize_api.errors")

ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]


class ErrorCode(StrEnum):
    """Stable error codes (part of the API contract): never rename or reuse; only add."""

    BAD_REQUEST = "BAD_REQUEST"
    CORS_PREFLIGHT_REJECTED = "CORS_PREFLIGHT_REJECTED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


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
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "Too Many Requests"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "Internal Server Error"),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "Service Unavailable"),
}

# Framework HTTP errors (routing, method, our own HTTPException) mapped onto codes.
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


class AppError(Exception):
    """Raise from services to return a problem response with a registered code."""

    def __init__(self, code: ErrorCode, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.headers = headers


def problem_response(
    code: ErrorCode,
    *,
    status: int | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the problem body. `status` overrides the registered one (framework errors)."""
    spec = ERROR_SPECS[code]
    if status is None or status == spec.status:
        status, title = spec.status, spec.title
    else:
        title = HTTPStatus(status).phrase
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code.value,
        "request_id": current_request_id(),
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled_error)


# Starlette types every handler as (Request, Exception); each re-raises anything it was not
# registered for, which also narrows the type without `assert`.


async def _app_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):
        raise exc
    return problem_response(exc.code, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]), "reason": str(error["type"])}
        for error in exc.errors()[:MAX_VALIDATION_ERRORS]
    ]
    return problem_response(ErrorCode.VALIDATION_FAILED, errors=errors)


async def _http_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    code = _CODE_FOR_STATUS.get(exc.status_code)
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.BAD_REQUEST
    return problem_response(code, status=exc.status_code, headers=exc.headers)


async def _unhandled_error(request: Request, exc: Exception) -> Response:
    route = request.scope.get("route")
    logger.error(
        "unhandled_error",
        exc_info=exc,
        extra={
            "request_id": current_request_id(),
            "route": getattr(route, "path", "unmatched"),
        },
    )
    return problem_response(ErrorCode.INTERNAL_ERROR)
