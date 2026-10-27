"""App factory: settings, lifespan, middleware and routers.

Run with `uvicorn --factory gize_api.main:create_app`. There is deliberately no module-level
app: building one needs the environment, and importing this module must not.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from gize_api.core.config import Environment, Settings, load_settings
from gize_api.core.errors import HandledErrorGuard, install_exception_handlers
from gize_api.core.logging import AccessLogMiddleware, configure_logging
from gize_api.core.middleware import (
    BodySizeLimitMiddleware,
    CORSGuardMiddleware,
    SecurityHeadersMiddleware,
)
from gize_api.core.request_id import RequestIdMiddleware
from gize_api.core.sentry import configure_sentry
from gize_api.core.telemetry import Telemetry, configure_telemetry

MAX_BODY_BYTES = 1024 * 1024

REDIS_TIMEOUT_SECONDS = 1.0
REDIS_MAX_CONNECTIONS = 50
REDIS_CLIENT_NAME = "gize-api"

CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"]
CORS_EXPOSED_HEADERS = ["X-Request-ID", "Retry-After"]
CORS_MAX_AGE_SECONDS = 600


class GizeAPI(FastAPI):
    """FastAPI with the cross-cutting layers wrapped around the whole stack.

    Middleware added with `add_middleware` runs inside Starlette's ServerErrorMiddleware, so
    it never sees the 500 for an unhandled error. Wrapping the built stack does. From the
    outside in: tracing (when configured) -> security headers -> request ID -> access log ->
    CORS guard -> CORS -> handled-error guard -> ServerErrorMiddleware -> `add_middleware`
    layers -> routes. So
    every response, including a 500, has the request ID and nosniff, is readable by allowed
    origins, and gets exactly one access-log line.
    """

    cors_allowed_origins: Sequence[str] = ()
    telemetry: Telemetry | None = None

    def build_middleware_stack(self) -> ASGIApp:
        app: ASGIApp = HandledErrorGuard(super().build_middleware_stack())
        app = CORSMiddleware(
            app,
            allow_origins=self.cors_allowed_origins,
            allow_methods=CORS_ALLOWED_METHODS,
            allow_headers=CORS_ALLOWED_HEADERS,
            expose_headers=CORS_EXPOSED_HEADERS,
            allow_credentials=False,
            max_age=CORS_MAX_AGE_SECONDS,
        )
        app = CORSGuardMiddleware(app, allowed_origins=self.cors_allowed_origins)
        app = SecurityHeadersMiddleware(RequestIdMiddleware(AccessLogMiddleware(app)))
        if self.telemetry is not None:
            app = self.telemetry.wrap(app, self.routes)
        return app


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Without `settings`, read them from the environment (fails fast)."""
    if settings is None:
        settings = load_settings()
    configure_logging(settings.log_level)
    if settings.app_env in {Environment.STAGING, Environment.PRODUCTION}:
        for name in settings.missing_observability():
            structlog.get_logger("gize_api.startup").warning("observability_disabled", setting=name)
    app = GizeAPI(
        title="Gize Admin API",
        lifespan=_lifespan(settings),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.telemetry = configure_telemetry(settings)
    configure_sentry(settings)
    app.cors_allowed_origins = settings.cors_allowed_origins
    install_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
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
            max_connections=REDIS_MAX_CONNECTIONS,
            client_name=REDIS_CLIENT_NAME,
        )
        app.state.redis = redis
        try:
            yield
        finally:
            del app.state.redis
            await redis.aclose()
            telemetry = getattr(app, "telemetry", None)
            if telemetry is not None:
                telemetry.shutdown()

    return lifespan
