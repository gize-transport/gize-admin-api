"""App factory: settings, lifespan, middleware and routers.

Run with `uvicorn --factory gize_api.main:create_app`. There is deliberately no module-level
app: building one needs the environment, and importing this module must not.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from gize_api.areas.router import router as area_routes
from gize_api.audit_log.router import router as audit_log_routes
from gize_api.companies.router import router as company_routes
from gize_api.core.config import Environment, Settings, load_settings
from gize_api.core.contract import finish_contract
from gize_api.core.db import create_engine, create_sessionmaker
from gize_api.core.errors import HandledErrorGuard, LastResortGuard, install_exception_handlers
from gize_api.core.jobs import create_job_redis
from gize_api.core.logging import AccessLogMiddleware, configure_logging
from gize_api.core.middleware import (
    BodySizeLimitMiddleware,
    CORSGuardMiddleware,
    SecurityHeadersMiddleware,
)
from gize_api.core.ratelimit import RateLimiter
from gize_api.core.request_id import RequestIdMiddleware
from gize_api.core.security import TokenVerifier
from gize_api.core.sentry import configure_sentry
from gize_api.core.telemetry import Telemetry, configure_telemetry
from gize_api.corridors.router import router as corridor_routes
from gize_api.health.readiness import Readiness
from gize_api.health.router import router as health_router
from gize_api.integrations.supabase_auth import SupabaseAuthAdmin
from gize_api.routes.router import router as route_routes
from gize_api.staff.router import router as staff_routes

MAX_BODY_BYTES = 1024 * 1024

REDIS_TIMEOUT_SECONDS = 1.0
REDIS_MAX_CONNECTIONS = 50
REDIS_CLIENT_NAME = "gize-api"

CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"]
CORS_EXPOSED_HEADERS = ["X-Request-ID", "Retry-After", "Idempotent-Replayed"]
CORS_MAX_AGE_SECONDS = 600


class GizeAPI(FastAPI):
    """FastAPI with the cross-cutting layers wrapped around the whole stack.

    Middleware added with `add_middleware` runs inside Starlette's ServerErrorMiddleware, so
    it never sees the 500 for an unhandled error. Wrapping the built stack does. From the
    outside in: tracing (when configured) -> last-resort guard -> security headers ->
    request ID -> access log ->
    CORS guard -> CORS -> handled-error guard -> ServerErrorMiddleware -> `add_middleware`
    layers -> routes. So
    every response, including a 500, has the request ID and nosniff, is readable by allowed
    origins, and gets exactly one access-log line.
    """

    cors_allowed_origins: Sequence[str] = ()
    telemetry: Telemetry | None = None

    def openapi(self) -> dict[str, Any]:
        """The contract: FastAPI's schema with our error responses (core/contract.py)."""
        if self.openapi_schema is None:
            self.openapi_schema = finish_contract(super().openapi())
        return self.openapi_schema

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
        app = LastResortGuard(app)
        if self.telemetry is not None:
            app = self.telemetry.wrap(app, self.routes)
        return app


# FastAPI's own OpenTelemetry layer (on by default once any global provider exists) would
# record raw URLs and exception text; ours is configured in core/telemetry.py instead.
_FASTAPI_NATIVE_TELEMETRY_OFF: TelemetryConfig = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


def create_app(settings: Settings | None = None) -> GizeAPI:
    """Build the API. Without `settings`, read them from the environment (fails fast)."""
    if settings is None:
        settings = load_settings()
    configure_logging(settings.log_level)
    # Production without them refuses to start (config.py) unless explicitly opted out.
    if settings.app_env in {Environment.STAGING, Environment.PRODUCTION}:
        for name in settings.missing_observability():
            structlog.get_logger("gize_api.startup").warning("observability_disabled", setting=name)
    app = GizeAPI(
        title="Gize Admin API",
        version="v1",  # each released contract is an api-vX.Y.Z tag (docs/api-contract.md)
        lifespan=_lifespan(settings),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        telemetry=_FASTAPI_NATIVE_TELEMETRY_OFF,
    )
    app.state.settings = settings
    app.telemetry = configure_telemetry(settings)
    configure_sentry(settings)
    app.cors_allowed_origins = settings.cors_allowed_origins
    install_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.include_router(health_router)
    app.include_router(staff_routes)
    app.include_router(audit_log_routes)
    app.include_router(area_routes)
    app.include_router(corridor_routes)
    app.include_router(company_routes)
    app.include_router(route_routes)
    return app


def _lifespan(settings: Settings) -> Callable[[GizeAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: GizeAPI) -> AsyncIterator[None]:
        # No connection is made here: the API starts while Postgres or Redis is down, and
        # /health/ready reports it.
        # A startup failure is logged here as type and frames; uvicorn's own report of it
        # (formatted traceback text) is suppressed by the logging filter.
        try:
            redis = create_job_redis(  # a Redis client that can also enqueue jobs
                settings.redis_url.get_secret_value(),
                timeout=REDIS_TIMEOUT_SECONDS,
                max_connections=REDIS_MAX_CONNECTIONS,
                client_name=REDIS_CLIENT_NAME,
            )
            app.state.redis = redis
            engine = create_engine(settings)
            app.state.engine = engine
            app.state.sessionmaker = create_sessionmaker(engine)
            telemetry = app.telemetry
            readiness = Readiness.for_settings(settings, telemetry)
            app.state.readiness = readiness
            app.state.rate_limiter = RateLimiter.for_settings(settings, telemetry=telemetry)
            app.state.token_verifier = TokenVerifier.for_settings(settings, telemetry=telemetry)
            # Staff invites need SUPABASE_SECRET_KEY; without it they answer 503.
            app.state.auth_admin = (
                None
                if settings.supabase_secret_key is None
                else SupabaseAuthAdmin.for_settings(settings, telemetry=telemetry)
            )
            if telemetry is not None:
                telemetry.instrument_redis(redis)
                telemetry.instrument_database(engine)
        except Exception:
            structlog.get_logger("gize_api.startup").error(
                "lifespan_failed", phase="startup", exc_info=True
            )
            raise
        try:
            yield
        finally:
            # Each step runs even if an earlier one fails.
            rate_limiter: RateLimiter = app.state.rate_limiter
            token_verifier: TokenVerifier = app.state.token_verifier
            auth_admin: SupabaseAuthAdmin | None = app.state.auth_admin
            del app.state.redis, app.state.engine, app.state.sessionmaker, app.state.readiness
            del app.state.rate_limiter, app.state.token_verifier, app.state.auth_admin
            try:
                await rate_limiter.aclose()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "rate_limiter_close_failed", exc_info=True
                )
            try:
                await token_verifier.aclose()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "token_verifier_close_failed", exc_info=True
                )
            try:
                if auth_admin is not None:
                    await auth_admin.aclose()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "auth_admin_close_failed", exc_info=True
                )
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "database_dispose_failed", exc_info=True
                )
            try:
                await readiness.close()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "readiness_close_failed", exc_info=True
                )
            try:
                if telemetry is not None:
                    telemetry.uninstrument_redis(redis)
                await redis.aclose()
            except Exception:  # noqa: BLE001 - shutdown must carry on; the failure is logged
                structlog.get_logger("gize_api.startup").warning(
                    "redis_close_failed", exc_info=True
                )
            if telemetry is not None:
                await telemetry.shutdown()

    return lifespan
