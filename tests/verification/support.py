"""Phone verification test helpers (GZ-031)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from gize_api.core.ratelimit import RateLimiter
from gize_api.main import GizeAPI
from tests.support import TEST_VERIFIED_PHONE_TOKEN_KEY, UNREACHABLE_REDIS_URL

TOKEN_KEY = TEST_VERIFIED_PHONE_TOKEN_KEY


@asynccontextmanager
async def broken_redis(app: GizeAPI) -> AsyncIterator[None]:
    """The app's limits counted in a Redis that refuses connections."""
    working: RateLimiter = app.state.rate_limiter
    settings = app.state.settings.model_copy(update={})
    broken = RateLimiter.for_settings(
        settings.model_copy(update={"redis_url": _secret(UNREACHABLE_REDIS_URL)}), telemetry=None
    )
    app.state.rate_limiter = broken
    try:
        yield
    finally:
        app.state.rate_limiter = working
        await broken.aclose()


def _secret(value: str) -> object:
    from pydantic import SecretStr

    return SecretStr(value)
