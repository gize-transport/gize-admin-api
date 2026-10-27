"""GZ-003: app factory, lifespan, CORS allowlist, body limit and security headers."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from redis.asyncio import Redis

from gize_api.core.config import ConfigError
from gize_api.main import MAX_BODY_BYTES, create_app
from tests.conftest import SettingsFactory

ALLOWED_ORIGIN = "https://admin.example.com"


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings())

    @app.post("/_test/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    return app


# --- factory and lifespan ---------------------------------------------------------------


def test_create_app_refuses_to_start_without_required_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("APP_ENV", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError):
        create_app()


def test_create_app_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    assert isinstance(create_app(), FastAPI)


async def test_lifespan_opens_redis_with_timeouts_and_closes_it(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        redis = app.state.redis
        assert isinstance(redis, Redis)
        kwargs = redis.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == 1.0
        assert kwargs["socket_connect_timeout"] == 1.0
    assert not hasattr(app.state, "redis")


async def test_starts_and_serves_with_redis_unreachable(app: FastAPI) -> None:
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.post("/_test/echo", content=b"{}")
    assert response.status_code == 200


# --- body size limit --------------------------------------------------------------------


async def test_body_at_limit_accepted(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post("/_test/echo", content=b"x" * MAX_BODY_BYTES)
    assert response.status_code == 200
    assert response.json() == {"size": MAX_BODY_BYTES}


async def test_declared_body_over_limit_rejected_413(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post("/_test/echo", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Payload Too Large",
        "status": 413,
        "code": "PAYLOAD_TOO_LARGE",
    }


async def test_streamed_body_over_limit_rejected_413(app: FastAPI) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * (MAX_BODY_BYTES // 4)

    async with _client(app) as client:
        response = await client.post("/_test/echo", content=chunks())
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_streamed_body_under_limit_reaches_handler(app: FastAPI) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 1000

    async with _client(app) as client:
        response = await client.post("/_test/echo", content=chunks())
    assert response.json() == {"size": 4000}


# --- security headers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("POST", "/_test/echo", b"{}", 200),
        ("GET", "/no-such-route", b"", 404),
        ("POST", "/_test/echo", b"x" * (MAX_BODY_BYTES + 1), 413),
    ],
)
async def test_nosniff_on_every_response(
    app: FastAPI, method: str, path: str, body: bytes, status: int
) -> None:
    async with _client(app) as client:
        response = await client.request(method, path, content=body)
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"


# --- CORS -------------------------------------------------------------------------------


async def test_allowed_origin_gets_cors_headers(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/_test/echo", content=b"{}", headers={"Origin": ALLOWED_ORIGIN}
        )
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


async def test_disallowed_origin_gets_no_cors_headers(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post(
            "/_test/echo", content=b"{}", headers={"Origin": "https://evil.example.com"}
        )
    assert response.status_code == 200
    # Starlette still lists exposed header names; without Allow-Origin the browser ignores
    # them and the response stays unreadable cross-origin.
    granting = {"access-control-allow-origin", "access-control-allow-credentials"}
    assert not granting & set(response.headers)
    assert "origin" in response.headers["vary"].lower()


async def test_preflight_from_allowed_origin(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.options(
            "/_test/echo",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "idempotency-key"):
        assert header in allowed


async def test_preflight_from_disallowed_origin_rejected(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.options(
            "/_test/echo",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_with_unlisted_header_rejected(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.options(
            "/_test/echo",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-forwarded-for",
            },
        )
    assert response.status_code == 400


# --- API docs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app_env", "expected"), [("local", 200), ("staging", 200), ("test", 404), ("production", 404)]
)
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_only_in_local_and_staging(
    make_settings: SettingsFactory, app_env: str, expected: int, path: str
) -> None:
    async with _client(create_app(make_settings(app_env=app_env))) as client:
        response = await client.get(path)
    assert response.status_code == expected
