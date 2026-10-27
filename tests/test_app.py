"""GZ-003: app factory, lifespan, CORS allowlist, body limit and security headers."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from redis.asyncio import Redis

from gize_api.core.config import ConfigError
from gize_api.main import (
    MAX_BODY_BYTES,
    REDIS_CLIENT_NAME,
    REDIS_MAX_CONNECTIONS,
    REDIS_TIMEOUT_SECONDS,
    create_app,
)
from tests.support import SettingsFactory

ALLOWED_ORIGIN = "https://admin.example.com"


def _client(app: FastAPI) -> httpx.AsyncClient:
    # Unhandled exceptions become 500 responses, as they would behind a real server.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings())

    @app.post("/_test/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("boom: internal detail")

    return app


# --- factory and lifespan ---------------------------------------------------------------


def test_create_app_refuses_to_start_without_required_env(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError):
        create_app()


def test_create_app_reads_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("APP_ENV", "test")
    clean_env.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    assert isinstance(create_app(), FastAPI)


async def test_lifespan_redis_client_settings(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        redis = app.state.redis
        assert isinstance(redis, Redis)
        pool = redis.connection_pool
        assert pool.connection_kwargs["socket_timeout"] == REDIS_TIMEOUT_SECONDS
        assert pool.connection_kwargs["socket_connect_timeout"] == REDIS_TIMEOUT_SECONDS
        assert pool.connection_kwargs["client_name"] == REDIS_CLIENT_NAME
        assert pool.max_connections == REDIS_MAX_CONNECTIONS
    assert not hasattr(app.state, "redis")


async def test_lifespan_opens_and_closes_real_redis_connections(
    make_settings: SettingsFactory, test_redis_url: str
) -> None:
    app = create_app(make_settings(redis_url=test_redis_url))
    async with Redis.from_url(test_redis_url, socket_timeout=1.0) as observer:

        async def ours() -> list[str]:
            clients = await observer.client_list()
            return [c["name"] for c in clients if c["name"] == REDIS_CLIENT_NAME]

        async with app.router.lifespan_context(app):
            assert await app.state.redis.ping() is True
            assert await ours() == [REDIS_CLIENT_NAME]
        assert await ours() == []


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
        ("GET", "/_test/crash", b"", 500),
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
    assert not [h for h in response.headers if h.startswith("access-control-")]
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
    assert not [h for h in response.headers if h.startswith("access-control-")]


async def test_unhandled_error_does_not_leak_detail(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash", headers={"Origin": ALLOWED_ORIGIN})
    assert response.status_code == 500
    assert "boom" not in response.text
    assert "RuntimeError" not in response.text


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
