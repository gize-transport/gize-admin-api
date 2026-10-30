"""Fixtures for staff management (GZ-013): the fake Supabase wired into `api_app`. The app
on committed sessions (`committed_api`) is in tests/conftest.py, shared by every domain."""

from collections.abc import AsyncIterator

import pytest

from gize_api.main import GizeAPI
from tests.staff.support import FakeSupabase
from tests.support import SettingsFactory


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
