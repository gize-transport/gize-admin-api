"""GZ-005: JSON logs with request context; secrets and personal data never written."""

import dataclasses
import json
import logging
import time
import uuid
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
    assert error["exception_type"] == "RuntimeError"
    assert "exception" not in error


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


@pytest.mark.parametrize(("app_env", "token", "warned"), [
    ("staging", None, True),
    ("staging", "afro-test-token-0123456789", False),
    ("local", None, False),
])  # fmt: skip
def test_startup_warns_when_staging_or_production_has_no_sms(
    make_settings: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
    app_env: str,
    token: str | None,
    warned: bool,
) -> None:
    """GZ-038: without AFROMESSAGE_TOKEN every SMS fails; say so at startup."""
    create_app(make_settings(app_env=app_env, afromessage_token=token))
    events = [line["event"] for line in _lines(capsys)]
    assert ("sms_disabled" in events) is warned


def test_no_observability_warning_in_local(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(app_env="local"))
    assert not [line for line in _lines(capsys) if line["event"] == "observability_disabled"]


# --- re-review hardening ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leaks"),
    [
        ("name='Abebe Kebede'", ["Abebe", "Kebede"]),
        ('password = "hunter 2"', ["hunter 2"]),
        ("rider_name=Abebe Kebede, seats=2", ["Abebe", "Kebede"]),
        ("<Obj2 name=Abebe Kebede>", ["Abebe", "Kebede"]),
        ("{'full_name': 'Abebe Kebede'}", ["Abebe", "Kebede"]),
        ("'password': b'hunter2'", ["hunter2"]),
        (
            "1 validation error for Rider\nfull_name\n  Value error [type=value_error, "
            "input_value='Abebe Kebede', input_type=str]",
            ["Abebe", "Kebede"],
        ),
        ("input_value={'otp': '482913', 'x': 1}, input_type=dict", ["482913"]),
        ("sms code 482913", ["482913"]),
        ("otp is 482913", ["482913"]),
        ("OTP 482913", ["482913"]),
        ("Your Gize verification code is 482913", ["482913"]),
        ("code=482913", ["482913"]),
        ("Authorization: Token abc123def", ["abc123def"]),
        ("Authorization: ApiKey abc123def", ["abc123def"]),
        ("https://user:p@ss@host/x", ["p@ss", "ss@host"]),
        (
            "pw=hunter2 pass=hunter3 passcode=4321 passphrase=open-sesame",
            ["hunter2", "hunter3", "4321", "open-sesame"],
        ),
        ("token=" + "x" * 600, ["x" * 20]),
    ],
)
def test_scrub_text_free_text_leaks_closed(text: str, leaks: list[str]) -> None:
    scrubbed = scrub_text(text)
    for leak in leaks:
        assert leak not in scrubbed, (text, scrubbed)


@pytest.mark.parametrize(
    "text",
    [
        "error code INTERNAL_ERROR returned",
        "status 404 after 12 ms",
        "route code R-12 has 14 seats",
        "pin the version 2026.10",
    ],
)
def test_scrub_text_leaves_error_codes_and_numbers(text: str) -> None:
    assert scrub_text(text) == text


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("phoneNumber", "0911223344", "09••••3344"),
        ("riderPhone", "0911223344", "09••••3344"),
        ("telephone", "0911223344", "09••••3344"),
        ("tel", "0911223344", "09••••3344"),
        ("fullName", "Abebe", REDACTED),
        ("firstName", "Abebe", REDACTED),
        ("fullname", "Abebe", REDACTED),
        ("lastname", "Kebede", REDACTED),
        ("surname", "Kebede", REDACTED),
        ("username", "abebe", REDACTED),
        ("emailAddress", "a@b.c", REDACTED),
        ("accessToken", "abc", REDACTED),
        ("refreshToken", "abc", REDACTED),
        ("authToken", "abc", REDACTED),
        ("sessionId", "abc", REDACTED),
        ("national_id_number", "ET1", REDACTED),
        ("kebeleId", "K1", REDACTED),
        ("pw", "x", REDACTED),
        ("passcode", "x", REDACTED),
        ("hash", "x", REDACTED),
        ("code", "482913", REDACTED),
        ("code", 482913, REDACTED),
        ("code", "INTERNAL_ERROR", "INTERNAL_ERROR"),
        ("phone", 911223344.0, "9••••3344"),
        ("amount", 251911223344, "2519••••3344"),
        ("seats", 14, 14),
    ],
)
def test_scrub_value_key_variants(key: str, value: object, expected: object) -> None:
    assert scrub_value(key, value) == expected


def test_dict_keys_are_scrubbed() -> None:
    assert scrub_value("by_phone", {"0911223344": 1}) == {"09••••3344": 1}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("payload", {"status": "sent to 0911223344"}),
        ("rider_id", {"phone": "0911223344"}),
        ("external_id", "0911223344"),
        ("status", "sent to 0911223344"),
        ("route", "/v1/x?phone=0911223344"),
    ],
)
def test_trusted_and_id_keys_cannot_carry_phones(key: str, value: object) -> None:
    assert "0911223344" not in json.dumps(scrub_value(key, value))


def test_containers_are_capped() -> None:
    scrubbed = scrub_value("items", list(range(500)))
    assert len(scrubbed) == 51
    assert scrubbed[-1] == "[+450 more]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00251911223344", "00251 9••••3344".replace(" ", "")),
        ("0911.223.344", "09••••3344"),
        ("(0911) 223344", "(09••••3344"),
        ("+251(0)911223344", "+2519••••3344"),
        ("otp_0911223344", "otp_09••••3344"),
    ],
)
def test_more_phone_formats(text: str, expected: str) -> None:
    assert mask_phone_numbers(text) == expected


def test_exceptions_are_logged_as_type_and_frames_never_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    def lookup() -> None:
        raise KeyError(f"rider Abebe Kebede {PHONE} otp {OTP}")

    try:
        try:
            lookup()
        except KeyError as exc:
            raise RuntimeError(f"wrapped {LOCAL_PHONE} children={['Liya', 'Sara']}") from exc
    except RuntimeError:
        structlog.get_logger("gize_api.test").exception("failed")
        logging.getLogger("some.library").exception("library failed")
    output = capsys.readouterr().err
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert len(lines) == 2  # one JSON line each: no traceback text appended
    for line in lines:
        assert line["exception_type"] == "RuntimeError"
        assert line["exception_chain"] == ["RuntimeError", "KeyError"]
        assert any(frame.endswith(" in lookup") for frame in line["exception_frames"])
        assert all(":" in frame for frame in line["exception_frames"])
    for leak in ("Abebe", "Kebede", PHONE, LOCAL_PHONE, OTP, "Liya", "Sara", "raise "):
        assert leak not in output, leak


@pytest.mark.parametrize("exc_info", [True, "instance", "tuple"])
def test_every_exc_info_form_is_formatted(
    capsys: pytest.CaptureFixture[str], exc_info: object
) -> None:
    configure_logging("INFO")
    try:
        raise ValueError(f"secret {PHONE}")
    except ValueError as exc:
        value = {"instance": exc, "tuple": (type(exc), exc, exc.__traceback__)}.get(
            str(exc_info), True
        )
        structlog.get_logger("gize_api.test").error("failed", exc_info=value)
    (line,) = _lines(capsys)
    assert line["exception_type"] == "ValueError"
    assert PHONE not in json.dumps(line)


def test_frames_are_bounded_and_paths_short(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    def deep(n: int) -> None:
        if n == 0:
            raise ValueError("x")
        deep(n - 1)

    try:
        deep(100)
    except ValueError:
        structlog.get_logger("gize_api.test").exception("failed")
    (line,) = _lines(capsys)
    assert len(line["exception_frames"]) <= 30
    assert all(not frame.startswith("/") for frame in line["exception_frames"])


async def test_unknown_methods_are_logged_as_other(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(app) as client:
        await client.request("FOO0911223344", "/_test/riders/1")
    (access,) = [line for line in _lines(capsys) if line["event"] == "request"]
    assert access["method"] == "_OTHER"


def test_dicts_are_capped() -> None:
    scrubbed = scrub_value("items", {f"k{i}": i for i in range(60)})
    assert len(scrubbed) == 51
    assert scrubbed["…"] == "[+10 more]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # a secret key inside an already-redacted value is skipped, not redacted twice
        ("name='Abebe password=hunter2' seats=2", "name='[REDACTED]' seats=2"),
        ("password=, seats=2", "password=, seats=2"),  # empty value: nothing to redact
        ("password='abc\nnext line", "password=[REDACTED]\nnext line"),  # unclosed quote
    ],
)
def test_assignment_edge_cases(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


def test_fractional_number_under_a_phone_key_is_redacted() -> None:
    assert scrub_value("phone", 911223344.5) == REDACTED


# --- third review: structured fields and library lines ----------------------------------


@pytest.mark.parametrize(
    ("text", "leaks"),
    [
        ("children=['Liya', 'Sara'] seats=2", ["Liya", "Sara"]),
        ("{'names': ('Abebe', 'Liya')}", ["Abebe", "Liya"]),
        ("{'guardian': {'first': 'Almaz'}}", ["Almaz"]),
        ("password=['hunter2']", ["hunter2"]),
        ("{'input': 'Abebe Kebede', 'loc': ('body',)}", ["Abebe", "Kebede"]),
        ('{"name": "Abebe \\"Abi\\" Kebede"}', ["Abi", "Kebede"]),
        ("to=%2B251911223344&text=Your%20code%20is%20482913", ["911223344", "482913"]),
        ("text=Your+code+is+482913", ["482913"]),
        ("{'body': 'line1\\n0911223344'}", ["0911223344"]),
        ("482913 is your Gize code", ["482913"]),
        ("code is: 482913", ["482913"]),
        ("code was 482913", ["482913"]),
        ("one-time password 482913", ["482913"]),
        ("OTPs 482913", ["482913"]),
        ("code: 482-913", ["482-913"]),
        ("codes 482913, 112233", ["482913", "112233"]),
        ("የማረጋገጫ ኮድ 482913", ["482913"]),
        ("phone=911223344", ["911223344"]),
        ('{"phone":911223344}', ["911223344"]),
        ("0 911 223 344", ["911 223 344"]),
        ("[(b'cookie', b'sid=1'), (b'x-api-key', b'k123')]", ["sid=1", "k123"]),
        ("('apikey', 'zz9')", ["zz9"]),
    ],
)
def test_third_review_text_leaks_closed(text: str, leaks: list[str]) -> None:
    scrubbed = scrub_text(text)
    for leak in leaks:
        assert leak not in scrubbed, (text, scrubbed)


@pytest.mark.parametrize(
    "text",
    [
        "route_code=1203",
        "route code 1203",
        "error_code=1045",
        "table_name=riders column_name=phone constraint_name=uq_route_code route_name=Bole",
        "Authorization: Bearer x",
    ],
)
def test_third_review_no_over_redaction_or_corruption(text: str) -> None:
    scrubbed = scrub_text(text)
    if text.startswith("Authorization"):
        assert scrubbed == "Authorization: [REDACTED]"
    else:
        assert scrubbed == text


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("input", "Abebe Kebede", REDACTED),
        ("input_value", "Abebe", REDACTED),
        ("home_area", "Bole", REDACTED),
        ("destination_area", "Piassa", REDACTED),
        ("first", "Abebe", REDACTED),
        ("last", "Kebede", REDACTED),
        ("family", "Kebede", REDACTED),
        ("route_code", "1203", "1203"),
        ("table_name", "riders", "riders"),
        (
            "headers",
            [(b"cookie", b"sid=1"), (b"accept", b"*/*")],
            [["cookie", REDACTED], ["accept", "*/*"]],
        ),
        pytest.param("big", 10**5000, "[INT]", id="huge-int"),
    ],
)
def test_third_review_value_cases(key: str, value: object, expected: object) -> None:
    assert scrub_value(key, value) == expected


def test_validation_errors_logged_structured_are_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    try:
        RiderModel.model_validate({"full_name": 5, "phone": PHONE, "seats": "Almaz Tesfaye"})
    except Exception as exc:  # noqa: BLE001 - pydantic.ValidationError
        structlog.get_logger("gize_api.test").warning("invalid", errors=exc.errors())  # type: ignore[attr-defined]
    output = capsys.readouterr().err
    assert "Almaz" not in output
    assert PHONE not in output


def test_truncated_text_never_keeps_a_partial_token() -> None:
    scrubbed = scrub_text("x" * (MAX_TEXT_LENGTH - 6) + " +251911223344 tail")
    assert "51911223344" not in scrubbed
    assert "1122" not in scrubbed


def test_scrubbing_has_a_per_event_budget(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    nested = [[["pw." * 1000 for _ in range(50)] for _ in range(50)] for _ in range(50)]
    started = time.monotonic()
    structlog.get_logger("gize_api.test").info("big", payload=nested, rows=nested)
    assert time.monotonic() - started < 2.0
    (line,) = _lines(capsys)
    assert "[TRUNCATED]" in json.dumps(line)


def test_hostile_text_is_fast() -> None:
    started = time.monotonic()
    for hostile in ("pw." * 2000, "password=" * 500, "a:" * 3000, "{" * 4000, "'" * 4000):
        scrub_text(hostile)
    assert time.monotonic() - started < 1.0


def test_security_event_never_raises(capsys: pytest.CaptureFixture[str]) -> None:
    class Exploding:
        def __repr__(self) -> str:
            raise RuntimeError("repr failed")

    configure_logging("INFO")
    log_security_event("token_rejected", value=Exploding())  # must not raise


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("trailing password=", "trailing password="),  # key at the very end: no value
        ("password='hunter2", "password=[REDACTED]"),  # unclosed quote at the end of text
        ("children=[['Liya'], {'n': ('Sara',)}] ok", "children=[REDACTED] ok"),  # nested
    ],
)
def test_value_span_edges(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


def test_exception_chain_is_bounded(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    try:
        try:
            try:
                try:
                    try:
                        try:
                            raise KeyError("a")
                        except KeyError as e:
                            raise ValueError("b") from e
                    except ValueError as e:
                        raise TypeError("c") from e
                except TypeError as e:
                    raise LookupError("d") from e
            except LookupError as e:
                raise OSError("e") from e
        except OSError as e:
            raise RuntimeError("f") from e
    except RuntimeError:
        structlog.get_logger("gize_api.test").exception("failed")
    (line,) = _lines(capsys)
    assert line["exception_chain"] == [
        "RuntimeError",
        "OSError",
        "LookupError",
        "TypeError",
        "ValueError",
    ]


def test_emails_in_text_are_redacted() -> None:
    assert scrub_text("Abebe's email is abebe.k+gize@example.com.") == (
        "Abebe's email is [REDACTED]."
    )


def test_large_containers_are_not_copied_whole() -> None:
    @dataclasses.dataclass
    class Big:
        rows: list[int]

    huge = Big(rows=list(range(2_000_000)))
    started = time.monotonic()
    scrubbed = scrub_value("big", huge)
    assert time.monotonic() - started < 0.2
    assert scrubbed["rows"][-1] == "[+1999950 more]"
    big_set = set(range(5_000_000))
    started = time.monotonic()
    assert len(scrub_value("s", big_set)) == 51
    assert time.monotonic() - started < 0.2


def test_common_scalar_types_become_text() -> None:
    import datetime as dt
    import enum
    from decimal import Decimal
    from pathlib import PurePosixPath

    class Status(enum.Enum):
        ACTIVE = "active"

    rider_id = uuid.UUID("8f2a0b1c-0903-5564-90bf-0911223344aa")
    assert scrub_value("status", Status.ACTIVE) == "active"
    assert scrub_value("rider_id", rider_id) == str(rider_id)
    assert scrub_value("on", dt.date(2026, 10, 28)) == "2026-10-28"
    assert scrub_value("price", Decimal("1200")) == "1200"
    assert scrub_value("path", PurePosixPath("/tmp/x")) == "/tmp/x"  # noqa: S108 - a value
