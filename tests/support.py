"""Shared test types and constants (import from here, not from conftest)."""

import base64
import json
from collections.abc import Callable

from gize_api.core.config import Settings

# A port nothing listens on: Redis calls fail fast and nothing real is touched.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

# Every variable Settings reads; cleared so the developer's shell cannot change a result.
SETTINGS_ENV_VARS = (
    "APP_ENV",
    "REDIS_URL",
    "CORS_ALLOWED_ORIGINS",
    "LOG_LEVEL",
    "SENTRY_DSN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "RELEASE",
    "OBSERVABILITY_REQUIRED",
    "OTEL_TRACES_SAMPLER_RATIO",
)

SettingsFactory = Callable[..., Settings]


def fake_jwt(subject: str = "test") -> str:
    """A syntactically valid, unsigned-looking JWT built at runtime.

    Tests need token-shaped strings to prove scrubbing; building them here keeps secret
    scanners (rightly) failing on any token literal committed to the repo.
    """

    def part(data: dict[str, str]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'sub': subject})}.c2lnbmF0dXJl"
