"""Shared fixtures."""

from collections.abc import Callable
from typing import Any

import pytest

from gize_api.core.config import Settings

# A port nothing listens on: Redis calls fail fast, nothing real is touched.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Build Settings from explicit values only, never from the developer's shell."""

    def factory(**overrides: Any) -> Settings:  # noqa: ANN401 - mirrors Settings' kwargs
        values: dict[str, Any] = {
            "app_env": "test",
            "redis_url": UNREACHABLE_REDIS_URL,
            "cors_allowed_origins": ["https://admin.example.com"],
            "log_level": "INFO",
        }
        values.update(overrides)
        return Settings(**values)

    return factory
