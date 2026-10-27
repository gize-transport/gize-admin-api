"""GZ-005: JSON logs with request context; secrets and personal data never written."""

import dataclasses
import json
import logging
import uuid
import warnings
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel

from gize_api.core.logging import (
    MAX_TEXT_LENGTH,
    REDACTED,
    configure_logging,
    log_security_event,
    mask_phone_numbers,
    scrub_text,
    scrub_value,
    set_staff_id,
)
from gize_api.core.request_id import REQUEST_ID_HEADER
from gize_api.main import create_app
from tests.support import SettingsFactory, fake_jwt

PHONE = "+251911223344"
LOCAL_PHONE = "0911223344"
TOKEN = fake_jwt("secret")
OTP = "482913"
STAFF_ID = "6f1c2d3e-0000-4000-8000-000000000001"


class RiderModel(BaseModel):
    full_name: str
    phone: str
    seats: int


@dataclasses.dataclass
class GuardianRecord:
    guardian_name: str
    phone: str


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

    @app.get("/_test/as-staff")
    async def as_staff() -> dict[str, str]:
        set_staff_id(STAFF_ID)  # what the M1 auth dependency will do
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
    assert access["level"] == "info"
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
    assert [(line["route"], line["status"], line["level"]) for line in access] == [
        ("unmatched", 404, "info"),
        ("/_test/crash", 500, "error"),
    ]


async def test_staff_id_on_access_line_and_reset_for_the_next_request(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        await client.get("/_test/as-staff")
        await client.get("/_test/riders/1")
    first, second = [line for line in _lines(capsys) if line["event"] == "request"]
    assert first["staff_id"] == STAFF_ID
    assert "staff_id" not in second


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


def test_trace_id_on_lines_inside_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    with TracerProvider().get_tracer("test").start_as_current_span("work") as span:
        structlog.get_logger("gize_api.test").info("inside")
    structlog.get_logger("gize_api.test").info("outside")
    inside, outside = _lines(capsys)
    assert inside["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert "trace_id" not in outside


def test_security_event_is_a_warning_with_event_field(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log_security_event("token_rejected", reason="expired", token=TOKEN)
    (line,) = _lines(capsys)
    assert line["level"] == "warning"
    assert line["event"] == "token_rejected"
    assert line["category"] == "security"
    assert line["reason"] == "expired"
    assert line["token"] == REDACTED


def test_level_filters_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    structlog.get_logger("gize_api.test").info("quiet")
    logging.getLogger("gize_api.test").info("quiet too")
    logging.getLogger("uvicorn.error").info("uvicorn follows LOG_LEVEL too")
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


@pytest.mark.parametrize("name", ["httpx", "httpcore", "urllib3", "asyncio"])
def test_chatty_libraries_are_quiet_below_warning(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    configure_logging("DEBUG")
    logging.getLogger(name).info("HTTP Request: GET https://sms.example/send?key=abc&to=0911")
    logging.getLogger(name).warning("%s warning", name)
    assert [line["event"] for line in _lines(capsys)] == [f"{name} warning"]


def test_python_warnings_go_through_the_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    # pytest intercepts warnings itself, so the two halves are checked separately: warnings
    # are routed to logging, and the py.warnings logger is scrubbed like any other.
    configure_logging("INFO")
    assert warnings.showwarning.__module__ == "logging"
    logging.getLogger("py.warnings").warning("UserWarning: calling 0911223344 is deprecated")
    (line,) = _lines(capsys)
    assert line["logger"] == "py.warnings"
    assert "0911223344" not in line["event"]


# --- secrets and personal data: end to end ----------------------------------------------


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
        rider=RiderModel(full_name="Abebe Kebede", phone=PHONE, seats=2),
        guardian=GuardianRecord(guardian_name="Almaz Tesfaye", phone=LOCAL_PHONE),
        error=ValueError(f"bad phone {PHONE} and otp={OTP}"),
        payload_repr=f"{{'password': 'hunter2-pass', 'otp': '{OTP}'}}",
        payload_json=f'{{"otp_code": "{OTP}"}}',
        database_url="postgresql://gize_app:S3cretPw@db.internal:5432/gize",
        nested={"phone": LOCAL_PHONE, "items": [f"call {PHONE}", {"token": TOKEN}]},
        tags={"0911223344", "+251 91 122 3344"},
    )
    logging.getLogger("some.library").warning("calling %s about %s", PHONE, LOCAL_PHONE)
    output = capsys.readouterr().err
    for secret in (
        TOKEN,
        OTP,
        PHONE,
        LOCAL_PHONE,
        "911223344",
        "91 122 3344",
        "hunter2-pass",
        "S3cretPw",
        "key@o1",
        "Abebe",
        "Kebede",
        "Almaz",
        "Liya",
    ):
        assert secret not in output, secret
    assert "+2519••••3344" in output
    assert "09••••3344" in output


# --- scrub_value / scrub_text units -----------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        # secrets: by key segment, any value type
        ("otp", 482913, REDACTED),
        ("sms_code", "482913", REDACTED),
        ("verification_code", "482913", REDACTED),
        ("pin", "1234", REDACTED),
        ("service_role_key", "abc", REDACTED),
        ("private_key", "abc", REDACTED),
        ("X-Api-Key", "abc", REDACTED),
        ("credential", "abc", REDACTED),
        ("session", "abc", REDACTED),
        ("jwt", "abc", REDACTED),
        # personal data
        ("rider_name", "Abebe", REDACTED),
        ("driver_name", "Abebe", REDACTED),
        ("names", ["Abebe", "Liya"], REDACTED),
        ("parent_name", "Almaz", REDACTED),
        ("dob", "2015-01-01", REDACTED),
        ("birth_date", "2015-01-01", REDACTED),
        ("address", "Bole", REDACTED),
        ("school", "Lycée", REDACTED),
        ("email", "a@b.c", REDACTED),
        ("national_id", "ET123", REDACTED),
        # not secrets, despite a secret-looking substring or segment
        ("otp_attempts", 3, 3),
        ("token_count", 2, 2),
        ("footprint", "small", "small"),
        ("session_expires_at", "2026-10-27T10:00:00Z", "2026-10-27T10:00:00Z"),
        # phones under phone keys: every format, ints too, idempotent
        ("phone", 251911223344, "2519••••3344"),
        ("phone", "911223344", "9••••3344"),
        ("guardian_phone", "+251 91 122 3344", "+2519••••3344"),
        ("phone", "+2519••••3344", "+2519••••3344"),
        ("phone", "not a number", REDACTED),
        # identifiers are never masked
        ("rider_id", "a0911223344b", "a0911223344b"),
        ("trace_id", "0911223344aa0911223344aa0911", "0911223344aa0911223344aa0911"),
        # plain values pass through
        ("seats", 14, 14),
        ("active", True, True),
        ("ratio", 0.5, 0.5),
        ("note", None, None),
    ],
)
def test_scrub_value_by_key(key: str, value: object, expected: object) -> None:
    assert scrub_value(key, value) == expected


def test_scrub_value_objects_are_scrubbed_through_their_fields() -> None:
    scrubbed = scrub_value("rider", RiderModel(full_name="Abebe Kebede", phone=PHONE, seats=2))
    assert scrubbed == {"full_name": REDACTED, "phone": "+2519••••3344", "seats": 2}
    record = scrub_value("g", GuardianRecord(guardian_name="Almaz", phone=LOCAL_PHONE))
    assert record == {"guardian_name": REDACTED, "phone": "09••••3344"}


def test_scrub_value_unknown_objects_become_scrubbed_text() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return f"Opaque(phone={PHONE}, password=hunter2)"

    text = scrub_value("thing", Opaque())
    assert isinstance(text, str)
    assert PHONE not in text
    assert "hunter2" not in text


def test_scrub_value_bytes_and_sets() -> None:
    assert scrub_value("blob", b"\x00secret") == "[BYTES 7]"
    assert sorted(scrub_value("tags", {LOCAL_PHONE, "ok"})) == ["09••••3344", "ok"]


def test_scrub_value_never_raises_on_cycles_or_depth() -> None:
    cyclic: dict[str, Any] = {"a": 1}
    cyclic["self"] = cyclic
    assert "[TRUNCATED]" in json.dumps(scrub_value("payload", cyclic))


def test_scrub_failure_never_breaks_logging(capsys: pytest.CaptureFixture[str]) -> None:
    class Exploding:
        def __repr__(self) -> str:
            raise RuntimeError("repr failed")

    configure_logging("INFO")
    structlog.get_logger("gize_api.test").info("weird", value=Exploding())
    (line,) = _lines(capsys)
    assert line["event"] == "log_scrub_failed"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("{'password': 'hunter2', 'otp': '482913'}", "hunter2"),
        ("{'password': 'hunter2', 'otp': '482913'}", "482913"),
        ('{"otp_code": "482913"}', "482913"),
        ("input_value={'otp': '482913', 'phone': 'x'}", "482913"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("authorization=Bearer abc.def.ghi", "abc.def.ghi"),
        ("postgresql://user:S3cret@host:5432/db", "S3cret"),
        ("rediss://:S3cret@cache:6380/0", "S3cret"),
        ("GET /send?key=abc123&to=x", "abc123"),
        ("cookie: session=deadbeef", "deadbeef"),
        ("sentry dsn=https://pub@o1.ingest.sentry.io/1", "pub@o1"),
        (f"token {TOKEN} used", TOKEN),
    ],
)
def test_scrub_text_removes_secrets(text: str, secret: str) -> None:
    assert secret not in scrub_text(text)


def test_scrub_text_keeps_ordinary_text() -> None:
    text = "route /v1/routes/{route_id} returned 404 after 12.5 ms; seats=14, otp_attempts=3"
    assert scrub_text(text) == text


def test_scrub_text_is_bounded() -> None:
    hostile = "password" * 50_000 + "tokenx" * 50_000 + "="
    scrubbed = scrub_text(hostile)
    assert len(scrubbed) <= MAX_TEXT_LENGTH + len("…[TRUNCATED]")
    assert scrubbed.endswith("…[TRUNCATED]")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+251911223344", "+2519••••3344"),
        ("+251711223344", "+2517••••3344"),
        ("251911223344", "2519••••3344"),
        ("0911223344", "09••••3344"),
        ("+251 91 122 3344", "+2519••••3344"),
        ("+251-911-223-344", "+2519••••3344"),
        ("0911 223 344", "09••••3344"),
        ("call +251911223344 or 0711223344.", "call +2519••••3344 or 07••••3344."),
        ("+14155552671", "+141••••2671"),
        ("order 12345 at 10:30", "order 12345 at 10:30"),
        ("a0911223344b", "a0911223344b"),  # inside an identifier: left alone
        ("not a phone 091122334455", "not a phone 091122334455"),
    ],
)
def test_mask_phone_numbers(text: str, expected: str) -> None:
    assert mask_phone_numbers(text) == expected


def test_uuids_are_never_corrupted() -> None:
    step = 0x0911223344A1B2C3D4E5F60718293A4B
    ids = [str(uuid.UUID(int=(n * step) % 2**128)) for n in range(1, 5000)]
    ids += [str(uuid.uuid4()) for _ in range(5000)]
    ids += ["8f2a0b1c-0903-5564-90bf-0911223344aa", "0911223344aa4bbb8ccc0911223344aa"]
    for value in ids:
        assert mask_phone_numbers(value) == value


def test_request_id_and_route_are_never_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.get_logger("gize_api.test").info(
        "x", request_id="a0911223344b0911223344c", route="/v1/x/{x_id}"
    )
    (line,) = _lines(capsys)
    assert line["request_id"] == "a0911223344b0911223344c"
    assert line["route"] == "/v1/x/{x_id}"


# --- startup ----------------------------------------------------------------------------


def test_startup_warns_in_staging_when_observability_is_off(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(app_env="staging"))
    warnings_logged = [line for line in _lines(capsys) if line["event"] == "observability_disabled"]
    assert [w["setting"] for w in warnings_logged] == ["sentry_dsn", "otel_exporter_otlp_endpoint"]
    assert all(w["level"] == "warning" for w in warnings_logged)


def test_production_opt_out_still_warns(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(app_env="production", observability_required=False))
    warnings_logged = [line for line in _lines(capsys) if line["event"] == "observability_disabled"]
    assert len(warnings_logged) == 2


def test_no_observability_warning_in_local(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(app_env="local"))
    assert not [line for line in _lines(capsys) if line["event"] == "observability_disabled"]
