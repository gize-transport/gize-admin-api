"""GZ-004: every error is problem+json with a stable code and the request ID, nothing more."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from starlette.types import Message, Receive, Scope, Send

from gize_api.core import errors
from gize_api.core.errors import (
    ERROR_SPECS,
    MAX_FIELD_PART_LENGTH,
    MAX_VALIDATION_ERRORS,
    PROBLEM_JSON,
    AppError,
    ErrorCode,
    HandledErrorGuard,
    ValidationReason,
)
from gize_api.core.request_id import REQUEST_ID_HEADER, current_request_id
from gize_api.main import MAX_BODY_BYTES, create_app
from tests.support import UNREACHABLE_REDIS_URL, SettingsFactory

ALLOWED_ORIGIN = "https://admin.example.com"
GENERATED_ID = re.compile(r"[0-9a-f]{32}")
PROBLEM_KEYS = {"type", "title", "status", "code", "request_id"}
VALIDATION_KEYS = PROBLEM_KEYS | {"errors", "errors_truncated"}


class Rider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=100)
    seats: int = Field(ge=1, le=20)
    tags: dict[str, int] = Field(default_factory=dict)


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings())

    @app.post("/_test/riders")
    async def create_rider(rider: Rider) -> dict[str, str]:
        return {"name": rider.name}

    @app.get("/_test/page")
    async def page(limit: int = Query(25, ge=1, le=100)) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("boom: password=hunter2 at db.internal")

    @app.get("/_test/redis-down")
    async def redis_down() -> None:
        async with Redis.from_url(UNREACHABLE_REDIS_URL, socket_connect_timeout=1.0) as redis:
            await redis.ping()

    @app.get("/_test/app-error")
    async def app_error() -> None:
        raise AppError(ErrorCode.NOT_FOUND)

    @app.get("/_test/http-error")
    async def http_error() -> None:
        raise HTTPException(status_code=400, detail="internal detail: table riders")

    @app.get("/_test/http-500")
    async def http_500() -> None:
        raise HTTPException(status_code=500, detail="internal detail")

    @app.get("/_test/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="unregistered status")

    @app.get("/_test/nonstandard")
    async def nonstandard() -> None:
        raise HTTPException(status_code=499, detail="client closed")

    @app.get("/_test/not-modified")
    async def not_modified() -> None:
        raise HTTPException(status_code=304)

    @app.get("/_test/request-id")
    async def request_id() -> dict[str, str | None]:
        await asyncio.sleep(0)  # yield to the other requests mid-handler
        return {"request_id": current_request_id()}

    @app.get("/_test/request-id-sync")
    def request_id_sync() -> dict[str, str | None]:
        return {"request_id": current_request_id()}

    @app.get("/_test/crash-after-start")
    async def crash_after_start() -> Response:
        async def body() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError("late failure")

        return StreamingResponse(body())

    return app


def _client(app: FastAPI, *, raise_app_exceptions: bool = False) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == "about:blank"
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
    return body


def _logged(capsys: pytest.CaptureFixture[str], event: str) -> list[dict[str, Any]]:
    """The rendered JSON log lines (what production writes) for one event."""
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    return [line for line in lines if line.get("event") == event]


# --- unhandled errors -------------------------------------------------------------------


async def test_unhandled_exception_is_generic_500(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash")
    body = _assert_problem(response, 500, "INTERNAL_ERROR")
    assert set(body) == PROBLEM_KEYS
    for leak in ("boom", "hunter2", "db.internal", "RuntimeError", "Traceback"):
        assert leak not in response.text


async def test_unhandled_exception_log_line_shows_request_id_and_route(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash", headers={REQUEST_ID_HEADER: "req-abc-12345"})
    assert response.headers[REQUEST_ID_HEADER] == "req-abc-12345"
    (line,) = _logged(capsys, "unhandled_error")
    assert line["request_id"] == "req-abc-12345"
    assert line["route"] == "/_test/crash"
    assert line["level"] == "error"
    assert line["exception_type"] == "RuntimeError"
    assert any(frame.endswith(" in crash") for frame in line["exception_frames"])
    assert "exception" not in line
    assert "hunter2" not in json.dumps(line)  # the message is never logged


async def test_unhandled_exception_logged_once_and_never_escapes_to_the_server(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app, raise_app_exceptions=True) as client:
        response = await client.get("/_test/crash")
    assert response.status_code == 500
    assert len(_logged(capsys, "unhandled_error")) == 1


async def test_failure_after_response_started_is_logged_and_left_incomplete(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driven at the ASGI level: httpx's test transport cannot model an aborted response.

    The guard logs (with the request ID) and returns without completing the response, so the
    server aborts the connection and the client never mistakes a truncated body for success.
    """
    sent: list[Message] = []

    requested = False

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # like a server: block until the client disconnects
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/_test/crash-after-start",
        "raw_path": b"/_test/crash-after-start",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)  # must not raise

    start = next(m for m in sent if m["type"] == "http.response.start")
    request_id = dict(start["headers"])[b"x-request-id"].decode()
    assert not any(m["type"] == "http.response.body" and not m.get("more_body") for m in sent)
    (line,) = _logged(capsys, "unhandled_error")
    assert line["request_id"] == request_id
    assert line["route"] == "/_test/crash-after-start"


async def test_unhandled_500_is_readable_cross_origin(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash", headers={"Origin": ALLOWED_ORIGIN})
    _assert_problem(response, 500, "INTERNAL_ERROR")
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_dependency_failure_is_generic_500(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/redis-down")
    _assert_problem(response, 500, "INTERNAL_ERROR")
    for leak in ("127.0.0.1", "Connection", "refused", "redis", "Redis"):
        assert leak not in response.text
    (line,) = _logged(capsys, "unhandled_error")
    assert line["request_id"] == response.headers[REQUEST_ID_HEADER]


# --- validation -------------------------------------------------------------------------


async def test_validation_error_lists_location_field_and_reason_only(app: FastAPI) -> None:
    payload = {"name": "x" * 101, "seats": 0, "nickname": "secret-value-xyz"}
    async with _client(app) as client:
        response = await client.post("/_test/riders", json=payload)
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert set(body) == VALIDATION_KEYS
    assert body["errors_truncated"] is False
    assert sorted(body["errors"], key=str) == sorted(
        [
            {"location": "body", "field": "/name", "reason": "too_long"},
            {"location": "body", "field": "/seats", "reason": "out_of_range"},
            {"location": "body", "field": "/nickname", "reason": "unknown_field"},
        ],
        key=str,
    )
    assert "secret-value-xyz" not in response.text
    assert "xxxxx" not in response.text


async def test_missing_field(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post("/_test/riders", json={"name": "Abebe"})
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert body["errors"] == [{"location": "body", "field": "/seats", "reason": "missing"}]


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b'{"name": "a", "seats": ', "invalid_json"), (b'"just a string"', "invalid_type")],
)
async def test_malformed_body_points_at_the_whole_body(
    app: FastAPI, content: bytes, reason: str
) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/_test/riders", content=content, headers={"content-type": "application/json"}
        )
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert body["errors"] == [{"location": "body", "field": "", "reason": reason}]


@pytest.mark.parametrize(("limit", "reason"), [("0", "out_of_range"), ("abc", "invalid_type")])
async def test_query_validation_names_the_parameter(app: FastAPI, limit: str, reason: str) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/page", params={"limit": limit})
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert body["errors"] == [{"location": "query", "field": "/limit", "reason": reason}]


async def test_field_pointer_is_unambiguous_and_escaped(app: FastAPI) -> None:
    payload = {"name": "Abebe", "seats": 2, "tags": {"a.b/c~d": "not-an-int"}}
    async with _client(app) as client:
        response = await client.post("/_test/riders", json=payload)
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert body["errors"] == [
        {"location": "body", "field": "/tags/a.b~1c~0d", "reason": "invalid_type"}
    ]


async def test_client_chosen_keys_are_truncated(app: FastAPI) -> None:
    long_key = "<script>" + "k" * 1000
    async with _client(app) as client:
        response = await client.post(
            "/_test/riders", json={"name": "Abebe", "seats": 2, long_key: 1}
        )
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    (error,) = body["errors"]
    assert error["reason"] == "unknown_field"
    assert len(error["field"]) <= 1 + MAX_FIELD_PART_LENGTH
    assert "k" * (MAX_FIELD_PART_LENGTH + 1) not in response.text


async def test_validation_errors_are_bounded_and_flagged(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())

    @app.post("/_test/many")
    async def many(items: list[Rider]) -> None:
        return None

    async with _client(app) as client:
        response = await client.post("/_test/many", json=[{}] * 200)
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert len(body["errors"]) == MAX_VALIDATION_ERRORS
    assert body["errors_truncated"] is True


@pytest.mark.parametrize(
    ("pydantic_type", "reason"),
    [
        ("missing", "missing"),
        ("extra_forbidden", "unknown_field"),
        ("string_too_long", "too_long"),
        ("too_short", "too_short"),
        ("less_than_equal", "out_of_range"),
        ("int_parsing", "invalid_type"),
        ("dict_type", "invalid_type"),
        ("string_pattern_mismatch", "invalid_format"),
        ("uuid_parsing", "invalid_format"),
        ("literal_error", "not_allowed"),
        ("enum", "not_allowed"),
        ("json_invalid", "invalid_json"),
        ("value_error", "invalid"),
        ("some_future_check", "invalid"),
    ],
)
def test_reason_vocabulary_is_ours_not_pydantics(pydantic_type: str, reason: str) -> None:
    assert errors.reason_for(pydantic_type) == ValidationReason(reason)


# --- HTTP errors ------------------------------------------------------------------------


async def test_unknown_route_is_404_problem(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/no/such/route")
    _assert_problem(response, 404, "NOT_FOUND")


async def test_wrong_method_is_405_problem_with_allow_header(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.delete("/_test/page")
    _assert_problem(response, 405, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "GET"


async def test_app_error_uses_registered_status(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/app-error")
    _assert_problem(response, 404, "NOT_FOUND")


async def test_http_exception_detail_never_reaches_client(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/http-error")
    _assert_problem(response, 400, "BAD_REQUEST")
    assert "internal detail" not in response.text


async def test_raised_http_500_is_logged(app: FastAPI, capsys: pytest.CaptureFixture[str]) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/http-500")
    _assert_problem(response, 500, "INTERNAL_ERROR")
    assert "internal detail" not in response.text
    (line,) = _logged(capsys, "http_error")
    assert line["status"] == 500
    assert line["route"] == "/_test/http-500"
    assert line["request_id"] == response.headers[REQUEST_ID_HEADER]


@pytest.mark.parametrize(
    ("path", "status", "title"),
    [("/_test/teapot", 418, "I'm a Teapot"), ("/_test/nonstandard", 499, "Bad Request")],
)
async def test_unregistered_status_keeps_status_with_generic_code(
    app: FastAPI, path: str, status: int, title: str
) -> None:
    async with _client(app) as client:
        response = await client.get(path)
    body = _assert_problem(response, status, "BAD_REQUEST")
    assert body["title"] == title
    assert "unregistered" not in response.text
    assert "client closed" not in response.text


async def test_non_error_http_exception_has_no_problem_body(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/not-modified")
    assert response.status_code == 304
    assert response.content == b""
    assert "content-type" not in response.headers


async def test_payload_too_large_carries_request_id(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post("/_test/riders", content=b"x" * (MAX_BODY_BYTES + 1))
    _assert_problem(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
        {
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-forwarded-for",
        },
    ],
)
async def test_refused_preflight_is_problem_json(app: FastAPI, headers: dict[str, str]) -> None:
    async with _client(app) as client:
        response = await client.options("/_test/riders", headers=headers)
    _assert_problem(response, 400, "CORS_PREFLIGHT_REJECTED")
    assert not [h for h in response.headers if h.startswith("access-control-")]


# --- request IDs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/_test/page", 200),
        ("GET", "/nope", 404),
        ("GET", "/_test/crash", 500),
        ("POST", "/_test/riders", 422),
    ],
)
async def test_every_response_has_a_generated_request_id(
    app: FastAPI, method: str, path: str, status: int
) -> None:
    async with _client(app) as client:
        response = await client.request(method, path)
    assert response.status_code == status
    assert GENERATED_ID.fullmatch(response.headers[REQUEST_ID_HEADER])


@pytest.mark.parametrize("incoming", ["abcd1234", "Req-2026.10.27_x", "a" * 64])
async def test_safe_incoming_request_id_is_kept(app: FastAPI, incoming: str) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/request-id", headers={REQUEST_ID_HEADER: incoming})
    assert response.headers[REQUEST_ID_HEADER] == incoming
    assert response.json() == {"request_id": incoming}


@pytest.mark.parametrize(
    "incoming",
    ["short", "a" * 65, "has space 123", "semi;colon12", "ünïcode-1234", "a/b/c/d/e/f", ""],
)
async def test_unsafe_incoming_request_id_is_replaced(app: FastAPI, incoming: str) -> None:
    async with _client(app) as client:
        response = await client.get(
            "/_test/request-id", headers={REQUEST_ID_HEADER.encode(): incoming.encode()}
        )
    assert GENERATED_ID.fullmatch(response.headers[REQUEST_ID_HEADER])
    assert response.json() == {"request_id": response.headers[REQUEST_ID_HEADER]}


@pytest.mark.parametrize("path", ["/_test/request-id", "/_test/request-id-sync"])
async def test_concurrent_requests_each_see_their_own_id(app: FastAPI, path: str) -> None:
    async with _client(app) as client:
        responses = await asyncio.gather(
            *(
                client.get(path, headers={REQUEST_ID_HEADER: f"concurrent-{i:04d}"})
                for i in range(20)
            )
        )
    for i, response in enumerate(responses):
        assert response.json() == {"request_id": f"concurrent-{i:04d}"}
    assert current_request_id() is None


# --- registry ---------------------------------------------------------------------------

# Codes and reasons are a public contract (docs/api-contract.md): never rename, reuse or
# change their status. Adding one means adding it here too.
EXPECTED_CODES = {
    "BAD_REQUEST": 400,
    "CORS_PREFLIGHT_REJECTED": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION_FAILED": 422,
    "INVALID_CURSOR": 422,
    "IDEMPOTENCY_KEY_REQUIRED": 428,
    "IDEMPOTENCY_KEY_MISMATCH": 422,
    "IDEMPOTENCY_IN_PROGRESS": 409,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
    "SERVICE_UNAVAILABLE": 503,
}
EXPECTED_REASONS = {
    "missing",
    "unknown_field",
    "too_long",
    "too_short",
    "out_of_range",
    "invalid_type",
    "invalid_format",
    "not_allowed",
    "invalid_json",
    "invalid",
}


def test_error_code_registry_is_stable() -> None:
    assert {code.value: ERROR_SPECS[code].status for code in ErrorCode} == EXPECTED_CODES


def test_validation_reasons_are_stable() -> None:
    assert {reason.value for reason in ValidationReason} == EXPECTED_REASONS


def test_every_code_has_a_title_and_value_equals_name() -> None:
    for code in ErrorCode:
        assert code.value == code.name
        assert ERROR_SPECS[code].title


Handler = Callable[[Request, Exception], Awaitable[Response]]


@pytest.mark.parametrize(
    "handler", [errors._app_error, errors._validation_error, errors._http_error]
)
async def test_handlers_re_raise_exceptions_they_were_not_registered_for(
    handler: Handler,
) -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with pytest.raises(LookupError):
        await handler(request, LookupError())


async def test_guard_re_raises_errors_no_handler_logged() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise LookupError("not handled anywhere")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    with pytest.raises(LookupError):
        await HandledErrorGuard(failing)({"type": "http", "state": {}}, receive, send)


async def test_guard_passes_non_http_scopes_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await HandledErrorGuard(inner)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
