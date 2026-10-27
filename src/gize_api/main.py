"""App factory: settings, lifespan, middleware and routers.

Run with `uvicorn --factory gize_api.main:create_app`. There is deliberately no module-level
app: building one needs the environment, and importing this module must not.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.middleware.cors import CORSMiddleware

from gize_api.core.config import Settings, load_settings
from gize_api.core.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware

MAX_BODY_BYTES = 1024 * 1024
REDIS_TIMEOUT_SECONDS = 1.0

CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"]
CORS_EXPOSED_HEADERS = ["X-Request-ID", "Retry-After"]
CORS_MAX_AGE_SECONDS = 600


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Without `settings`, read them from the environment (fails fast)."""
    settings = settings or load_settings()
    app = FastAPI(
        title="Gize Admin API",
        lifespan=_lifespan(settings),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    # Added innermost first: security headers wrap CORS, which wraps the body limit, so a
    # 413 still carries both.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        expose_headers=CORS_EXPOSED_HEADERS,
        allow_credentials=False,
        max_age=CORS_MAX_AGE_SECONDS,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # No connection is made here: the API starts while Redis is down, and readiness
        # (GZ-007) reports it.
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        )
        app.state.redis = redis
        try:
            yield
        finally:
            del app.state.redis
            await redis.aclose()

    return lifespan
