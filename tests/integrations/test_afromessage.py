"""GZ-038: SMS through AfroMessage (https://www.afromessage.com/developers).

`POST https://api.afromessage.com/api/send` with `Authorization: Bearer <token>` and a JSON
body (`to`, `message`, and `sender`/`from` when set). AfroMessage answers
`{"acknowledge": "success", "response": {"message_id": ...}}` or
`{"acknowledge": "error", "response": {"errors": [...]}}`. Each answer maps to GZ-030's
outcome: the message ID, or an `SmsSendError` kind. Never retried; the token, the phone
and the text never reach a log or an error.
"""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import structlog
from pydantic import SecretStr, ValidationError

from gize_api.core.config import Environment
from gize_api.core.http import OutboundClient
from gize_api.integrations.afromessage import AFROMESSAGE_HOST, AfroMessageSender
from gize_api.integrations.sms import FakeSmsSender, SmsSendError, UnconfiguredSmsSender
from gize_api.integrations.sms_senders import sms_sender_for
from gize_api.main import create_app
from tests.support import SettingsFactory, public_resolver

TOKEN = "afro-test-token-0123456789"  # noqa: S105 - a test value, never a real token
PHONE = "+251911234567"
TEXT = "Gize: your code is 123456. It expires in 10 minutes."
MESSAGE_ID = "9ab2867c-96ce-4405-b890-8d35d52c8e01"

Handler = Callable[[httpx.Request], httpx.Response]


def _success(message_id: object = MESSAGE_ID) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "acknowledge": "success",
            "response": {
                "status": "Send in progress...",
                "message_id": message_id,
                "message": TEXT,
                "to": PHONE,
            },
        },
    )


def _error(*errors: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"acknowledge": "error", "response": {"errors": errors}})


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
async def sender_with(
    make_settings: SettingsFactory, requests: list[httpx.Request]
) -> AsyncIterator[Callable[..., AfroMessageSender]]:
    """A sender whose every request gets `answer` (a response, or an exception to raise)."""
    made: list[AfroMessageSender] = []

    def build(answer: httpx.Response | Exception, **settings: Any) -> AfroMessageSender:  # noqa: ANN401
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer

        client = OutboundClient(
            allowed_hosts={AFROMESSAGE_HOST},
            resolver=public_resolver,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        made.append(
            AfroMessageSender.for_settings(
                make_settings(afromessage_token=TOKEN, **settings), telemetry=None, outbound=client
            )
        )
        return made[-1]

    yield build
    for sender in made:
        await sender.aclose()


async def test_a_message_is_sent_with_the_token_and_answers_its_id(
    sender_with: Callable[..., AfroMessageSender], requests: list[httpx.Request]
) -> None:
    sender = sender_with(
        _success(), afromessage_sender_name="GIZE", afromessage_identifier_id="e80ad9d8"
    )
    assert await sender.send(PHONE, TEXT) == MESSAGE_ID
    (request,) = requests
    # The client connects to the address it checked, naming the host in the Host header.
    assert (request.method, request.url.path) == ("POST", "/api/send")
    assert request.headers["Host"] == "api.afromessage.com"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "to": PHONE,
        "message": TEXT,
        "sender": "GIZE",
        "from": "e80ad9d8",
    }


async def test_without_a_sender_name_or_identifier_they_are_left_out(
    sender_with: Callable[..., AfroMessageSender], requests: list[httpx.Request]
) -> None:
    """AfroMessage then uses the account's defaults (its own name: beta testing only)."""
    await sender_with(_success()).send(PHONE, TEXT)
    assert json.loads(requests[0].content) == {"to": PHONE, "message": TEXT}


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (_error("Unable to send your message. Message content is empty..."), "rejected"),
        (_error("Invalid phone number", status=400), "rejected"),
        (_error("Invalid phone number", status=422), "rejected"),
        (httpx.Response(401, json={"message": "Unauthenticated."}), "unavailable"),
        (httpx.Response(403), "unavailable"),
        (httpx.Response(429), "unavailable"),
        (httpx.Response(500), "timeout"),  # it, or a proxy in front, may have acted
        (httpx.Response(502), "timeout"),
        (httpx.Response(504), "timeout"),
        (httpx.Response(503, text="down"), "unavailable"),
        (httpx.Response(404), "unavailable"),
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ConnectTimeout("slow"), "unavailable"),  # never reached: nothing sent
        (httpx.ReadTimeout("slow"), "timeout"),  # sent, no answer: maybe sent
        (httpx.WriteTimeout("slow"), "timeout"),
        (httpx.ReadError("reset"), "timeout"),
        (httpx.RemoteProtocolError("dropped"), "timeout"),
        # Accepted, maybe sent, but we can't tell: as uncertain as a timeout.
        (httpx.Response(200, text="<html>maintenance</html>"), "timeout"),
        (httpx.Response(200, content=b"x" * (1024 * 1024 + 1)), "timeout"),  # too large
        (httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"x"), "timeout"),
        (httpx.Response(200, json={"acknowledge": "success", "response": {}}), "timeout"),
        (_success(message_id=None), "timeout"),
        (_success(message_id="x" * 101), "timeout"),
        (httpx.Response(200, json=["success"]), "timeout"),
        (httpx.Response(200, json={"acknowledge": "maybe"}), "timeout"),
    ],
    ids=[
        "error answer", "400", "422", "token refused", "403", "429", "500", "502", "504",
        "503", "404", "unreachable", "connect timed out", "read timed out",
        "write timed out", "read error", "dropped", "not json", "too large", "compressed",
        "no response", "no message id", "message id too long", "not an object",
        "unknown acknowledge",
    ],
)  # fmt: skip
async def test_each_failure_is_a_kind_without_secrets(
    sender_with: Callable[..., AfroMessageSender], answer: httpx.Response | Exception, kind: str
) -> None:
    with structlog.testing.capture_logs() as logs, pytest.raises(SmsSendError) as exc_info:
        await sender_with(answer).send(PHONE, TEXT)
    assert exc_info.value.kind == kind
    text = repr(logs) + repr(exc_info.value) + str(exc_info.value.__cause__)
    for secret in (TOKEN, PHONE, PHONE[4:], "123456"):
        assert secret not in text


async def test_a_refusal_is_logged_with_its_reason_masked(
    sender_with: Callable[..., AfroMessageSender],
) -> None:
    """The reason helps fix a bad number or sender name; a number in it is masked."""
    answer = _error(
        f"Invalid recipient {PHONE}", "Sender name not approved", f"Could not send: {TEXT}"
    )
    with structlog.testing.capture_logs() as logs, pytest.raises(SmsSendError):
        await sender_with(answer).send(PHONE, TEXT)
    (entry,) = [log for log in logs if log["event"] == "afromessage_refused"]
    assert entry["log_level"] == "warning"
    assert "Sender name not approved" in entry["errors"]
    assert PHONE not in repr(entry)
    assert "123456" not in repr(entry)  # an echoed code is scrubbed too


async def test_a_post_is_never_retried(
    sender_with: Callable[..., AfroMessageSender], requests: list[httpx.Request]
) -> None:
    """A 503 may still have sent the SMS: the next try is the caller's, as a new event."""
    with pytest.raises(SmsSendError):
        await sender_with(httpx.Response(503)).send(PHONE, TEXT)
    assert len(requests) == 1


async def test_the_real_client_talks_only_to_afromessage(make_settings: SettingsFactory) -> None:
    sender = AfroMessageSender.for_settings(make_settings(afromessage_token=TOKEN), telemetry=None)
    try:
        assert sender._client._allowed == frozenset({("api.afromessage.com", 443)})
    finally:
        await sender.aclose()


def test_the_sender_needs_a_token(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValueError, match="AFROMESSAGE_TOKEN"):
        AfroMessageSender.for_settings(make_settings(), telemetry=None)


async def test_a_refusal_without_reasons_is_still_rejected(
    sender_with: Callable[..., AfroMessageSender],
) -> None:
    answer = httpx.Response(200, json={"acknowledge": "error", "response": {"errors": "x"}})
    with structlog.testing.capture_logs() as logs, pytest.raises(SmsSendError) as exc_info:
        await sender_with(answer).send(PHONE, TEXT)
    assert exc_info.value.kind == "rejected"
    assert [log["errors"] for log in logs if log["event"] == "afromessage_refused"] == [[]]


def test_the_token_never_shows_in_a_repr(make_settings: SettingsFactory) -> None:
    settings = make_settings(afromessage_token=TOKEN)
    assert TOKEN not in repr(settings)
    assert isinstance(settings.afromessage_token, SecretStr)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("afromessage_token", "short"),
        ("afromessage_token", "has spaces in it 0123456789"),
        ("afromessage_sender_name", ""),
        ("afromessage_sender_name", "x" * 21),
        ("afromessage_sender_name", "GIZE\n"),
        ("afromessage_identifier_id", "not an id!"),
    ],
)
def test_bad_settings_are_refused_at_startup(
    make_settings: SettingsFactory, field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    "app_env", [Environment.LOCAL, Environment.STAGING, Environment.PRODUCTION]
)
def test_with_a_token_every_environment_but_tests_sends_through_afromessage(
    make_settings: SettingsFactory, app_env: Environment
) -> None:
    settings = make_settings(afromessage_token=TOKEN).model_copy(update={"app_env": app_env})
    assert type(sms_sender_for(settings, telemetry=None)) is AfroMessageSender


def test_tests_never_send_real_sms(make_settings: SettingsFactory) -> None:
    settings = make_settings(afromessage_token=TOKEN)  # APP_ENV=test
    assert type(sms_sender_for(settings, telemetry=None)) is not AfroMessageSender


def test_without_a_token_staging_still_has_no_sender(make_settings: SettingsFactory) -> None:
    settings = make_settings().model_copy(update={"app_env": Environment.STAGING})
    assert type(sms_sender_for(settings, telemetry=None)) is UnconfiguredSmsSender


async def test_a_failing_sender_close_does_not_stop_shutdown(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failing(self: FakeSmsSender) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(FakeSmsSender, "aclose", failing)
    app = create_app(make_settings())  # tests: the fake sender
    async with app.router.lifespan_context(app):
        pass
    assert '"sms_sender_close_failed"' in capsys.readouterr().err
