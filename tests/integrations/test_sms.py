"""GZ-030: the SMS senders. No provider is chosen yet, so local and test runs use a fake,
and staging and production have no sender: every send fails as `unavailable`, never
silently dropping a code into a fake.
"""

import pytest

from gize_api.core.config import Environment
from gize_api.integrations.sms import (
    FAKE_SENT_KEPT,
    FakeSmsSender,
    SmsSendError,
    UnconfiguredSmsSender,
    sms_sender_for,
)
from tests.support import SettingsFactory

PHONE = "+251911234567"


async def test_the_fake_keeps_what_it_sent_and_answers_an_id() -> None:
    sender = FakeSmsSender()
    first = await sender.send(PHONE, "one")
    second = await sender.send(PHONE, "two")
    assert sender.sent == [(PHONE, "one"), (PHONE, "two")]
    assert first != second
    assert first.startswith("fake-")


async def test_the_fake_keeps_only_the_latest_messages() -> None:
    sender = FakeSmsSender()
    for n in range(FAKE_SENT_KEPT + 5):
        await sender.send(PHONE, str(n))
    assert len(sender.sent) == FAKE_SENT_KEPT
    assert sender.sent[-1] == (PHONE, str(FAKE_SENT_KEPT + 4))


async def test_locally_the_fake_shows_the_text_with_the_phone_masked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A developer needs the code to sign up locally; the number is masked as in logs."""
    await FakeSmsSender(echo=True).send(PHONE, "Your Gize code is 123456")
    err = capsys.readouterr().err
    assert "Your Gize code is 123456" in err
    assert PHONE not in err
    assert "4567" in err  # the masked form keeps the last digits


async def test_without_echo_the_fake_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    await FakeSmsSender().send(PHONE, "Your Gize code is 123456")
    assert capsys.readouterr() == ("", "")


async def test_the_unconfigured_sender_always_fails_as_unavailable() -> None:
    with pytest.raises(SmsSendError) as exc_info:
        await UnconfiguredSmsSender().send(PHONE, "text")
    assert exc_info.value.kind == "unavailable"


def test_an_unknown_failure_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown SMS failure kind"):
        SmsSendError("lost")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("app_env", "kind", "echo"),
    [
        (Environment.LOCAL, FakeSmsSender, True),
        (Environment.TEST, FakeSmsSender, False),
        (Environment.STAGING, UnconfiguredSmsSender, None),
        (Environment.PRODUCTION, UnconfiguredSmsSender, None),
    ],
)
def test_each_environment_gets_its_sender(
    make_settings: SettingsFactory, app_env: Environment, kind: type, echo: bool | None
) -> None:
    settings = make_settings().model_copy(update={"app_env": app_env})
    sender = sms_sender_for(settings)
    assert type(sender) is kind
    if echo is not None:
        assert isinstance(sender, FakeSmsSender)
        assert sender.echo is echo
