"""RFC 9457 problem details (`application/problem+json`).

Clients get a stable machine `code` and a generic title; never exception text or internals.
GZ-004 adds the request ID, the exception handlers and the rest of the code registry.
"""

from enum import StrEnum

from starlette.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"


class ErrorCode(StrEnum):
    """Stable error codes. Never rename or reuse one; add new ones."""

    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"


_STATUS_AND_TITLE: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.PAYLOAD_TOO_LARGE: (413, "Payload Too Large"),
}


def problem_response(code: ErrorCode) -> JSONResponse:
    status, title = _STATUS_AND_TITLE[code]
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "code": code.value},
        status_code=status,
        media_type=PROBLEM_JSON,
    )
