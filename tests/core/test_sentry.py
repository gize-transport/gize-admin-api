"""GZ-005: Sentry receives errors and security events with the request ID, nothing personal."""

import json
from collections.abc import Iterator
from typing import Any, ClassVar, cast

import httpx
import pytest
import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event

from gize_api.core.logging import log_security_event
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


class FailingTransport(Transport):
    def capture_envelope(self, envelope: Envelope) -> None:
        raise ConnectionError("sentry unreachable")


def _reset_sentry() -> None:
    """Back to a disabled client. A bare `init()` would patch Sentry's integrations into
    Starlette, logging and httpx process-wide and change every later test."""
    sentry_sdk.get_client().close()
    sentry_sdk.init(default_integrations=False, auto_enabling_integrations=False)


@pytest.fixture
def sentry_enabled() -> Iterator[None]:
    CapturingTransport.envelopes = []
    yield
    _reset_sentry()


def _events() -> list[dict[str, Any]]:
    sentry_sdk.flush()
    return [
        item.payload.json
        for envelope in CapturingTransport.envelopes
        for item in envelope.items
        if item.type == "event" and item.payload.json is not None
    ]


def _crashing_app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings(sentry_dsn=DSN, release="2026.10.27-abc123"))

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError(f"failed for {PHONE} using {TOKEN}")

    return app


def test_disabled_without_dsn(make_settings: SettingsFactory) -> None:
    assert configure_sentry(make_settings()) is False


@pytest.mark.usefixtures("sentry_enabled")
async def test_unhandled_error_reaches_sentry_scrubbed_with_request_id(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(sentry_dsn=DSN, release="2026.10.27-abc123")
    assert configure_sentry(settings, transport=CapturingTransport) is True
    app = _crashing_app(make_settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/_test/crash?phone=0911223344",
            headers={"Authorization": f"Bearer {TOKEN}", "Cookie": f"s={TOKEN}"},
        )
    (event,) = _events()
    serialized = json.dumps(event)
    assert event["tags"]["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert event["tags"]["route"] == "/_test/crash"
    assert event["environment"] == "test"
    assert event["release"] == "2026.10.27-abc123"
    assert event["sdk"]["name"].startswith("sentry.python")  # metadata left intact
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "RuntimeError"
    assert "value" not in exception  # the message is never sent
    for frame in exception["stacktrace"]["frames"]:
        assert not {"context_line", "pre_context", "post_context", "vars"} & set(frame)
    for secret in (PHONE, "0911223344", TOKEN, "911223344"):
        assert secret not in serialized


@pytest.mark.usefixtures("sentry_enabled")
def test_security_events_reach_sentry_as_scrubbed_warnings(make_settings: SettingsFactory) -> None:
    configure_sentry(make_settings(sentry_dsn=DSN), transport=CapturingTransport)
    log_security_event("token_rejected", reason="expired", token=TOKEN, phone=PHONE)
    (event,) = _events()
    assert event["level"] == "warning"
    assert event["message"] == "token_rejected"
    assert event["tags"]["category"] == "security"
    serialized = json.dumps(event)
    assert TOKEN not in serialized
    assert PHONE not in serialized
    assert "expired" in serialized


@pytest.mark.usefixtures("sentry_enabled")
def test_no_trace_headers_to_other_hosts_and_no_breadcrumbs(make_settings: SettingsFactory) -> None:
    configure_sentry(make_settings(sentry_dsn=DSN), transport=CapturingTransport)
    options = sentry_sdk.get_client().options
    assert options["trace_propagation_targets"] == []
    assert options["max_breadcrumbs"] == 0
    assert options["auto_enabling_integrations"] is False


@pytest.mark.usefixtures("sentry_enabled")
async def test_sentry_failure_does_not_affect_the_response(make_settings: SettingsFactory) -> None:
    configure_sentry(make_settings(sentry_dsn=DSN), transport=FailingTransport)
    app = _crashing_app(make_settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/crash")
    sentry_sdk.flush()
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"


def test_scrub_event_scrubs_content_and_keeps_metadata() -> None:
    event: dict[str, Any] = {
        "event_id": "0911223344aa4bbb8ccc0911223344aa",
        "sdk": {"name": "sentry.python.fastapi", "version": "2.73.0"},
        "contexts": {
            "runtime": {"name": "CPython", "version": "3.12.16"},
            "trace": {
                "trace_id": "0911223344aa4bbb8ccc0911223344aa",
                "span_id": "0911223344aa4bbb",
            },
        },
        "message": f"call {PHONE}",
        "logentry": {"message": f"call {PHONE}", "params": [TOKEN]},
        "exception": {
            "values": [
                {"type": "ValueError", "value": f"bad {PHONE} {TOKEN}"},
                {"type": "SystemExit", "value": None},
            ]
        },
        "request": {
            "url": "http://test/x?phone=0911223344",
            "query_string": "phone=0911223344",
            "headers": {"Authorization": f"Bearer {TOKEN}", "Cookie": "a=b", "Accept": "*/*"},
            "cookies": {"session": TOKEN},
            "data": {"full_name": "Abebe Kebede"},
        },
        "extra": {"access_token": TOKEN, "note": f"phone {PHONE}"},
        "tags": {"request_id": "abc", "note": f"phone {PHONE}"},
        "breadcrumbs": {"values": [{"message": f"sms to {PHONE}"}]},
        "user": {"ip_address": "10.0.0.1", "email": "a@b.c"},
    }
    result = scrub_event(cast(Event, event), {})
    assert result is not None
    scrubbed = cast(dict[str, Any], result)
    assert "user" not in scrubbed
    content = json.dumps(
        {k: v for k, v in scrubbed.items() if k not in ("event_id", "sdk", "contexts")}
    )
    for secret in (PHONE, "0911223344", TOKEN, "Abebe", "10.0.0.1", "a@b.c"):
        assert secret not in content
    assert scrubbed["request"]["headers"]["Accept"] == "*/*"
    assert scrubbed["event_id"] == event["event_id"]
    assert scrubbed["sdk"]["name"] == "sentry.python.fastapi"
    assert scrubbed["contexts"]["runtime"]["name"] == "CPython"
    assert scrubbed["contexts"]["trace"]["trace_id"] == "0911223344aa4bbb8ccc0911223344aa"


def test_scrub_event_scrubs_custom_contexts_but_not_sdk_ones() -> None:
    event: dict[str, Any] = {
        "contexts": {
            "rider": {"full_name": "Abebe Kebede", "phone": PHONE},
            "runtime": {"name": "CPython"},
            "trace": {"trace_id": "0911223344aa4bbb8ccc0911223344aa"},
        }
    }
    result = scrub_event(cast(Event, event), {})
    assert result is not None
    contexts = cast(dict[str, Any], result)["contexts"]
    assert "Abebe" not in json.dumps(contexts["rider"])
    assert PHONE not in json.dumps(contexts["rider"])
    assert contexts["runtime"] == {"name": "CPython"}
    assert contexts["trace"]["trace_id"] == "0911223344aa4bbb8ccc0911223344aa"


def test_reset_leaves_no_sentry_integrations_behind(make_settings: SettingsFactory) -> None:
    configure_sentry(make_settings(sentry_dsn=DSN), transport=CapturingTransport)
    _reset_sentry()
    client = sentry_sdk.get_client()
    assert not client.options.get("dsn")
    assert not client.integrations


def test_scrub_event_without_contexts() -> None:
    result = scrub_event(cast(Event, {"message": f"call {PHONE}"}), {})
    assert result is not None
    assert PHONE not in json.dumps(result)
