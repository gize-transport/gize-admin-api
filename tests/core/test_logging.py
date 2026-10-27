"""GZ-005: JSON logs with request context; secrets and personal data never written."""

import json
import logging
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI

from gize_api.core.logging import (
    bind_staff_id,
    configure_logging,
    log_security_event,
    mask_phone_numbers,
)
from gize_api.core.request_id import REQUEST_ID_HEADER
from gize_api.main import create_app
from tests.support import SettingsFactory, fake_jwt

PHONE = "+251911223344"
LOCAL_PHONE = "0911223344"
TOKEN = fake_jwt("secret")
OTP = "482913"


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.strip()]


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings(log_level="DEBUG"))

    @app.get("/_test/riders/{rider_id}")
    async def rider(rider_id: str) -> dict[str, str]:
        structlog.get_logger("gize_api.test").info(
            "rider_viewed", rider_id=rider_id, phone=PHONE, full_name="Abebe Kebede"
        )
        return {"ok": "yes"}

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError(f"lookup failed for {PHONE} with token={TOKEN}")

    return app


# --- format and context -----------------------------------------------------------------


async def test_every_line_is_json_with_request_context(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/riders/42")
    lines = _lines(capsys)
    viewed = next(line for line in lines if line["event"] == "rider_viewed")
    assert viewed["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert viewed["level"] == "info"
    assert viewed["logger"] == "gize_api.test"
    assert viewed["timestamp"].endswith("Z")


async def test_one_access_log_line_per_request(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/riders/42", params={"phone": PHONE})
    (access,) = [line for line in _lines(capsys) if line["event"] == "request"]
    assert access["method"] == "GET"
    assert access["route"] == "/_test/riders/{rider_id}"  # template, never the raw path
    assert access["status"] == 200
    assert isinstance(access["duration_ms"], float)
    assert access["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "staff_id" not in access


async def test_access_log_for_unmatched_route_and_500(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        await client.get("/no/such/path/0911223344")
        await client.get("/_test/crash")
    access = [line for line in _lines(capsys) if line["event"] == "request"]
    assert [(line["route"], line["status"]) for line in access] == [
        ("unmatched", 404),
        ("/_test/crash", 500),
    ]


async def test_unhandled_error_logged_as_json_with_request_id(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        response = await client.get("/_test/crash")
    (error,) = [line for line in _lines(capsys) if line["event"] == "unhandled_error"]
    assert error["level"] == "error"
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert error["route"] == "/_test/crash"
    assert "RuntimeError" in error["exception"]


def test_staff_id_is_bound_to_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    with bind_staff_id("6f1c2d3e-0000-4000-8000-000000000001"):
        structlog.get_logger("gize_api.test").info("did_something")
    structlog.get_logger("gize_api.test").info("after")
    first, after = _lines(capsys)
    assert first["staff_id"] == "6f1c2d3e-0000-4000-8000-000000000001"
    assert "staff_id" not in after


def test_security_event_is_a_warning_with_event_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log_security_event("token_rejected", reason="expired")
    (line,) = _lines(capsys)
    assert line["level"] == "warning"
    assert line["event"] == "token_rejected"
    assert line["category"] == "security"
    assert line["reason"] == "expired"


def test_level_filters_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    structlog.get_logger("gize_api.test").info("quiet")
    logging.getLogger("gize_api.test").info("quiet too")
    structlog.get_logger("gize_api.test").warning("loud")
    assert [line["event"] for line in _lines(capsys)] == ["loud"]


def test_stdlib_and_uvicorn_loggers_render_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("Started server process")
    logging.getLogger("uvicorn.access").info('10.0.0.1:5000 - "GET /x?phone=0911223344" 200')
    assert not logging.getLogger("uvicorn.access").hasHandlers()
    logging.getLogger("some.library").warning("library says %s", "hello")
    events = [(line["logger"], line["event"]) for line in _lines(capsys)]
    assert events == [
        ("uvicorn.error", "Started server process"),
        ("some.library", "library says hello"),
    ]


# --- secrets and personal data ----------------------------------------------------------


async def test_tokens_otps_names_and_phones_never_written(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        await client.get(
            "/_test/riders/42",
            headers={"Authorization": f"Bearer {TOKEN}", "Cookie": f"session={TOKEN}"},
        )
        await client.get("/_test/crash")
    log = structlog.get_logger("gize_api.test")
    log.info(
        "otp_checked",
        otp=OTP,
        otp_code=OTP,
        access_token=TOKEN,
        authorization=f"Bearer {TOKEN}",
        password="hunter2-pass",  # noqa: S106 - the value under test
        sentry_dsn="https://key@o1.ingest.sentry.io/1",
        guardian_name="Almaz Tesfaye",
        child_name="Liya",
        nested={"phone": LOCAL_PHONE, "items": [f"call {PHONE}", {"token": TOKEN}]},
    )
    logging.getLogger("some.library").warning("calling %s about %s", PHONE, LOCAL_PHONE)
    output = capsys.readouterr().err
    for secret in (
        TOKEN,
        OTP,
        PHONE,
        LOCAL_PHONE,
        "911223344",
        "hunter2-pass",
        "key@o1",
        "Abebe",
        "Kebede",
        "Almaz",
        "Liya",
    ):
        assert secret not in output, secret
    assert "+2519••••3344" in output
    assert "09••••3344" in output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+251911223344", "+2519••••3344"),
        ("+251711223344", "+2517••••3344"),
        ("251911223344", "2519••••3344"),
        ("0911223344", "09••••3344"),
        ("call +251911223344 or 0711223344.", "call +2519••••3344 or 07••••3344."),
        ("+14155552671", "+141••••2671"),
        ("order 12345 at 10:30", "order 12345 at 10:30"),
        ("a0911223344b", "a09••••3344b"),
        ("not a phone 091122334455", "not a phone 091122334455"),
    ],
)
def test_mask_phone_numbers(text: str, expected: str) -> None:
    assert mask_phone_numbers(text) == expected


def test_request_id_and_route_are_never_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.get_logger("gize_api.test").info(
        "x", request_id="a0911223344b0911223344c", route="/v1/x/0911223344"
    )
    (line,) = _lines(capsys)
    assert line["request_id"] == "a0911223344b0911223344c"
    assert line["route"] == "/v1/x/0911223344"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_startup_warns_when_observability_is_off(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str], app_env: str
) -> None:
    create_app(make_settings(app_env=app_env))
    warnings = [line for line in _lines(capsys) if line["event"] == "observability_disabled"]
    assert [w["setting"] for w in warnings] == ["sentry_dsn", "otel_exporter_otlp_endpoint"]
    assert all(w["level"] == "warning" for w in warnings)


def test_no_observability_warning_in_local(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(app_env="local"))
    assert not [line for line in _lines(capsys) if line["event"] == "observability_disabled"]
