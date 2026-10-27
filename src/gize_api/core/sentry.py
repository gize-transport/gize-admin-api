"""Sentry error reporting: off unless SENTRY_DSN is set; never sends personal data.

The unhandled-error handler (core/errors.py) reports explicitly, tagged with the request ID
and route. Sentry's automatic Starlette/FastAPI/logging integrations are disabled: our
handled-error guard stops the re-raise they would see, and error logs would double-report.
Every event and breadcrumb passes through the same scrubber as the logs.
"""

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from gize_api.core.config import Settings
from gize_api.core.logging import scrub_value

# Event sections that only ever hold personal data.
_DROPPED_SECTIONS = ("user",)


def configure_sentry(settings: Settings, *, transport: type[Transport] | None = None) -> bool:
    """Initialise Sentry once per process if a DSN is configured.

    Sentry is process-global: an already active client is kept. `transport` replaces the
    network (tests).
    """
    if settings.sentry_dsn is None:
        return False
    if sentry_sdk.is_initialized():
        return True
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.app_env.value,
        release=settings.release,
        # Collect as little as possible; scrub_event is a second layer, not the only one.
        data_collection={
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_headers": {
                "request": {"mode": "allowlist", "terms": ["accept", "content-type", "user-agent"]}
            },
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "database_query_data": False,
            "stack_frame_variables": False,
        },
        traces_sample_rate=None,  # traces go to OpenTelemetry
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
        disabled_integrations=[StarletteIntegration, FastApiIntegration, LoggingIntegration],
        transport=transport,
    )
    return True


def report_exception(exc: BaseException, *, request_id: str | None, route: str) -> None:
    """Send an unhandled exception to Sentry (no-op when Sentry is off)."""
    sentry_sdk.capture_exception(exc, tags={"request_id": request_id or "none", "route": route})


def scrub_event(event: Any, hint: Any) -> Any:  # noqa: ANN401 - Sentry's event dict
    for section in _DROPPED_SECTIONS:
        event.pop(section, None)
    return {key: scrub_value(key, value) for key, value in event.items()}


def scrub_breadcrumb(crumb: Any, hint: Any) -> Any:  # noqa: ANN401 - Sentry's breadcrumb
    return {key: scrub_value(key, value) for key, value in crumb.items()}
