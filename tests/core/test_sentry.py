"""GZ-005: Sentry receives errors with the request ID and nothing personal."""

import json
from collections.abc import Iterator
from typing import Any, ClassVar

import httpx
import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from gize_api.core.request_id import REQUEST_ID_HEADER
from gize_api.core.sentry import configure_sentry, scrub_event
from gize_api.main import create_app
from tests.support import SettingsFactory, fake_jwt

PHONE = "+251911223344"
TOKEN = fake_jwt("x")
DSN = "https://public@sentry.example.invalid/1"


class CapturingTransport(Transport):
    envelopes: ClassVar[list[Envelope]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        CapturingTransport.envelopes.append(envelope)


@pytest.fixture
def sentry_events(make_settings: SettingsFactory) -> Iterator[list[dict[str, Any]]]:
    CapturingTransport.envelopes = []
    events: list[dict[str, Any]] = []
    yield events
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # back to a disabled client


def _events() -> list[dict[str, Any]]:
    return [
        item.payload.json
        for envelope in CapturingTransport.envelopes
        for item in envelope.items
        if item.type == "event" and item.payload.json is not None
    ]


def test_disabled_without_dsn(make_settings: SettingsFactory) -> None:
    assert configure_sentry(make_settings()) is False


async def test_unhandled_error_reaches_sentry_scrubbed_with_request_id(
    make_settings: SettingsFactory, sentry_events: list[dict[str, Any]]
) -> None:
    settings = make_settings(sentry_dsn=DSN, release="2026.10.27-abc123")
    assert configure_sentry(settings, transport=CapturingTransport) is True
    app = create_app(settings)

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError(f"failed for {PHONE} using {TOKEN}")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/_test/crash?phone=0911223344",
            headers={"Authorization": f"Bearer {TOKEN}", "Cookie": f"s={TOKEN}"},
        )
    sentry_sdk.flush()
    (event,) = _events()
    serialized = json.dumps(event)
    assert event["tags"]["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert event["environment"] == "test"
    assert event["release"] == "2026.10.27-abc123"
    for secret in (PHONE, "0911223344", TOKEN, "911223344"):
        assert secret not in serialized


def test_scrub_event_masks_and_redacts_everywhere() -> None:
    event: dict[str, Any] = {
        "message": f"call {PHONE}",
        "exception": {"values": [{"type": "ValueError", "value": f"bad {PHONE} {TOKEN}"}]},
        "request": {
            "url": "http://test/x?phone=0911223344",
            "query_string": "phone=0911223344",
            "headers": {"Authorization": f"Bearer {TOKEN}", "Cookie": "a=b", "Accept": "*/*"},
            "cookies": {"session": TOKEN},
            "data": {"full_name": "Abebe Kebede"},
        },
        "extra": {"access_token": TOKEN, "note": f"phone {PHONE}"},
        "breadcrumbs": {"values": [{"message": f"sms to {PHONE}"}]},
        "user": {"ip_address": "10.0.0.1", "email": "a@b.c"},
    }
    scrubbed = scrub_event(event, {})
    assert scrubbed is not None
    serialized = json.dumps(scrubbed)
    for secret in (PHONE, "0911223344", TOKEN, "Abebe", "10.0.0.1", "a@b.c"):
        assert secret not in serialized
    assert scrubbed["request"]["headers"]["Accept"] == "*/*"
