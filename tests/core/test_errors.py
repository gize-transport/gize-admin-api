"""GZ-004: every error is problem+json with a stable code and the request ID, nothing more."""

import logging
import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from gize_api.core import errors
from gize_api.core.errors import ERROR_SPECS, PROBLEM_JSON, AppError, ErrorCode
from gize_api.core.request_id import REQUEST_ID_HEADER, current_request_id
from gize_api.main import MAX_BODY_BYTES, create_app
from tests.support import SettingsFactory

ALLOWED_ORIGIN = "https://admin.example.com"
GENERATED_ID = re.compile(r"[0-9a-f]{32}")
PROBLEM_KEYS = {"type", "title", "status", "code", "request_id"}


class Rider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=100)
    seats: int = Field(ge=1, le=20)


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

    @app.get("/_test/app-error")
    async def app_error() -> None:
        raise AppError(ErrorCode.NOT_FOUND)

    @app.get("/_test/http-error")
    async def http_error() -> None:
        raise HTTPException(status_code=400, detail="internal detail: table riders")

    @app.get("/_test/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="unregistered status")

    @app.get("/_test/request-id")
    async def request_id() -> dict[str, str | None]:
        return {"request_id": current_request_id()}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
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


# --- unhandled errors -------------------------------------------------------------------


async def test_unhandled_exception_is_generic_500(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash")
    body = _assert_problem(response, 500, "INTERNAL_ERROR")
    assert set(body) == PROBLEM_KEYS
    for leak in ("boom", "hunter2", "db.internal", "RuntimeError", "Traceback"):
        assert leak not in response.text


async def test_unhandled_exception_logged_with_same_request_id(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="gize_api")
    async with _client(app) as client:
        response = await client.get("/_test/crash", headers={REQUEST_ID_HEADER: "req-abc-12345"})
    assert response.headers[REQUEST_ID_HEADER] == "req-abc-12345"
    records = [r for r in caplog.records if r.getMessage() == "unhandled_error"]
    assert len(records) == 1
    assert records[0].__dict__["request_id"] == "req-abc-12345"
    assert records[0].__dict__["route"] == "/_test/crash"
    assert records[0].exc_info is not None


# --- validation -------------------------------------------------------------------------


async def test_validation_error_lists_field_and_reason_only(app: FastAPI) -> None:
    payload = {"name": "x" * 101, "seats": 0, "nickname": "secret-value-xyz"}
    async with _client(app) as client:
        response = await client.post("/_test/riders", json=payload)
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert set(body) == PROBLEM_KEYS | {"errors"}
    assert sorted(body["errors"], key=str) == sorted(
        [
            {"field": "body.name", "reason": "string_too_long"},
            {"field": "body.seats", "reason": "greater_than_equal"},
            {"field": "body.nickname", "reason": "extra_forbidden"},
        ],
        key=str,
    )
    assert "secret-value-xyz" not in response.text
    assert "xxxxx" not in response.text


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b'{"name": "a", "seats": ', "json_invalid"), (b'"just a string"', "model_attributes_type")],
)
async def test_malformed_body_is_validation_error(
    app: FastAPI, content: bytes, reason: str
) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/_test/riders", content=content, headers={"content-type": "application/json"}
        )
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert [e["reason"] for e in body["errors"]] == [reason]


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
async def test_query_validation_names_the_parameter(app: FastAPI, limit: str) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/page", params={"limit": limit})
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert [e["field"] for e in body["errors"]] == ["query.limit"]


async def test_validation_errors_are_bounded(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())

    @app.post("/_test/many")
    async def many(items: list[Rider]) -> None:
        return None

    async with _client(app) as client:
        response = await client.post("/_test/many", json=[{}] * 200)
    body = _assert_problem(response, 422, "VALIDATION_FAILED")
    assert len(body["errors"]) == 50


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


async def test_unregistered_status_keeps_status_with_generic_code(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/teapot")
    body = _assert_problem(response, 418, "BAD_REQUEST")
    assert body["title"] == "I'm a Teapot"
    assert "unregistered" not in response.text


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


async def test_request_ids_differ_and_do_not_leak_between_requests(app: FastAPI) -> None:
    async with _client(app) as client:
        first = await client.get("/_test/request-id")
        second = await client.get("/_test/request-id")
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]
    assert current_request_id() is None


# --- registry ---------------------------------------------------------------------------

# Codes are a public contract (docs/api-contract.md): never rename, reuse or change status.
# Adding a code means adding it here too.
EXPECTED_CODES = {
    "BAD_REQUEST": 400,
    "CORS_PREFLIGHT_REJECTED": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION_FAILED": 422,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
    "SERVICE_UNAVAILABLE": 503,
}


def test_error_code_registry_is_stable() -> None:
    assert {code.value: ERROR_SPECS[code].status for code in ErrorCode} == EXPECTED_CODES


def test_every_code_has_a_title_and_value_equals_name() -> None:
    for code in ErrorCode:
        assert code.value == code.name
        assert ERROR_SPECS[code].title


@pytest.mark.parametrize(
    "handler", [errors._app_error, errors._validation_error, errors._http_error]
)
async def test_handlers_re_raise_exceptions_they_were_not_registered_for(
    handler: errors.ExceptionHandler,
) -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with pytest.raises(LookupError):
        await handler(request, LookupError())
