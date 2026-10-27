"""Shared test types and constants (import from here, not from conftest)."""

from collections.abc import Callable

from gize_api.core.config import Settings

# A port nothing listens on: Redis calls fail fast and nothing real is touched.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

# Every variable Settings reads; cleared so the developer's shell cannot change a result.
SETTINGS_ENV_VARS = ("APP_ENV", "REDIS_URL", "CORS_ALLOWED_ORIGINS", "LOG_LEVEL")

SettingsFactory = Callable[..., Settings]
