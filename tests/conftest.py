"""Shared fixtures."""

from pathlib import Path
from typing import Any

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from gize_api.core.config import Settings
from tests.support import SETTINGS_ENV_VARS, UNREACHABLE_REDIS_URL, SettingsFactory

ROOT = Path(__file__).resolve().parent.parent


class ServiceUrls(BaseSettings):
    """Where the real test services live: the environment (CI) or the local `.env`."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    test_redis_url: str | None = None


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Build Settings from explicit values only, never from the developer's shell."""

    def factory(**overrides: Any) -> Settings:  # noqa: ANN401 - mirrors Settings' kwargs
        values: dict[str, Any] = {
            "app_env": "test",
            "redis_url": UNREACHABLE_REDIS_URL,
            "cors_allowed_origins": ("https://admin.example.com",),
            "log_level": "INFO",
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture(scope="session")
def test_redis_url() -> str:
    """A real Redis for tests (CI service or local compose). Never skipped: fails if unset."""
    url = ServiceUrls().test_redis_url
    if not url:
        pytest.fail("TEST_REDIS_URL is not set: add it to .env (see .env.example) or export it")
    return url
