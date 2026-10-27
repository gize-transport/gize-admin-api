"""Sentry error reporting: off unless SENTRY_DSN is set; never sends personal data.

- The unhandled-error handler (core/errors.py) reports explicitly, tagged with the request
  ID and route; security events (core/logging.py) arrive as warning events.
- Sentry's own Starlette/FastAPI/logging integrations and auto-enabled integrations are
  off: our handled-error guard stops the re-raise they would see, error logs would
  double-report, and outbound-HTTP integrations would leak URLs into breadcrumbs.
- No breadcrumbs, no trace headers to other hosts, minimal data collection.
- Content sections of every event pass through the same scrubber as the logs; metadata
  (event_id, sdk, contexts) is left intact so Sentry accepts the event.
"""

from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint

from gize_api.core.config import Settings
from gize_api.core.logging import scrub_text, scrub_value

# Sections that carry request or application content; everything else is SDK metadata.
_CONTENT_SECTIONS = ("message", "logentry", "extra", "tags", "request", "breadcrumbs")
# Contexts the SDK fills itself; any other context (set_context) is application content.
_SDK_CONTEXTS = frozenset(
    {"trace", "runtime", "os", "device", "app", "browser", "culture", "cloud_resource", "otel"}
)


def configure_sentry(settings: Settings, *, transport: type[Transport] | None = None) -> bool:
    """Initialise Sentry once per process if a DSN is configured.

    Sentry is process-global: an already active client is kept. `transport` replaces the
    network (tests).
    """
    if settings.sentry_dsn is None:
        return False
    client = sentry_sdk.get_client()
    if client.is_active() and client.options.get("dsn"):  # a DSN-less client is "active" too
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
        trace_propagation_targets=[],  # never add sentry-trace/baggage to outbound requests
        auto_enabling_integrations=False,
        disabled_integrations=[StarletteIntegration, FastApiIntegration, LoggingIntegration],
        max_breadcrumbs=0,
        before_send=scrub_event,
        transport=transport,
    )
    return True


def report_exception(exc: BaseException, *, request_id: str | None, route: str) -> None:
    """Send an unhandled exception to Sentry (no-op when Sentry is off)."""
    sentry_sdk.capture_exception(exc, tags={"request_id": request_id or "none", "route": route})


def scrub_event(event: Event, hint: Hint) -> Event | None:
    data = cast(dict[str, Any], event)
    data.pop("user", None)
    for section in _CONTENT_SECTIONS:
        if section in data:
            data[section] = scrub_value(section, data[section])
    contexts = data.get("contexts")
    if isinstance(contexts, dict):
        for name, context in contexts.items():
            if name not in _SDK_CONTEXTS:
                contexts[name] = scrub_value(name, context)
    for value in data.get("exception", {}).get("values", []):
        if isinstance(value.get("value"), str):
            value["value"] = scrub_text(value["value"], keep_tail=True)
    return event
