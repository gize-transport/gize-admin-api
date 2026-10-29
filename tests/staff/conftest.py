"""Fixtures for staff management (GZ-013): the fake Supabase wired into the app, and the
app on committed sessions for tests of requests running in parallel."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.ratelimit import RateLimiter
from gize_api.main import GizeAPI, create_app
from tests.staff.support import FakeSupabase
from tests.support import SettingsFactory
from tests.tokens import fake_verifier


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
async def supabase(
    api_app: GizeAPI, make_settings: SettingsFactory, fake_supabase: FakeSupabase
) -> AsyncIterator[FakeSupabase]:
    """`api_app` (on the rolled-back `db_session`) inviting through the fake."""
    client = fake_supabase.client(make_settings)
    api_app.state.auth_admin = client
    try:
        yield fake_supabase
    finally:
        await client.aclose()


@pytest.fixture
async def committed_api(
    make_settings: SettingsFactory,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    test_redis_url: str,
    fake_supabase: FakeSupabase,
) -> AsyncIterator[httpx.AsyncClient]:
    """The app on sessions that really commit: each request has its own connection."""
    app = create_app(make_settings())
    app.state.sessionmaker = committed_sessionmaker
    app.state.token_verifier = fake_verifier()
    app.state.rate_limiter = RateLimiter.for_settings(
        make_settings(redis_url=test_redis_url),
        telemetry=None,
        key_prefix=f"gize-test-{uuid.uuid4()}",
    )
    app.state.auth_admin = fake_supabase.client(make_settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        await app.state.auth_admin.aclose()
        await app.state.rate_limiter.aclose()
