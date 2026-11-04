"""GZ-030: sending an SMS once per event, inline (the free plan has no worker).

`send_once` claims the event's key in `gize.sms_messages`, sends outside any transaction,
then records the outcome. The key makes a retried request harmless: the SMS goes out at
most once, whatever happened the first time. The text never reaches the table (it holds a
code). Runs on committed rows: the claim must hold across connections.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.integrations.sms import FakeSmsSender, SmsFailure, SmsSendError
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from gize_api.sms.service import send_once

PHONE = "+251911234567"
TEXT = "Your Gize code is 123456"


def _key() -> str:
    return f"otp:{uuid.uuid4()}"


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[SmsMessage]:
    async with sessions() as session, session.begin():
        return list((await session.scalars(select(SmsMessage))).all())


class FailingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def send(self, to: str, text: str) -> str:
        self.calls += 1
        raise self.error


async def test_it_sends_and_records_the_outcome_without_the_text(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSmsSender()
    key = _key()
    delivery = await send_once(
        committed_sessionmaker, sender, key=key, purpose=SmsPurpose.PHONE_VERIFICATION,
        to=PHONE, text=TEXT,
    )  # fmt: skip
    assert (delivery.status, delivery.first_attempt) == (SmsStatus.SENT, True)
    assert sender.sent == [(PHONE, TEXT)]
    (row,) = await _rows(committed_sessionmaker)
    assert (row.dedup_key, row.purpose, row.to_phone, row.status, row.error) == (
        key, SmsPurpose.PHONE_VERIFICATION, PHONE, SmsStatus.SENT, None,
    )  # fmt: skip
    assert row.provider_message_id is not None
    assert TEXT not in repr([getattr(row, c.key) for c in SmsMessage.__table__.columns])


async def test_the_same_key_never_sends_twice(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSmsSender()
    key = _key()
    for _ in range(2):
        delivery = await send_once(
            committed_sessionmaker, sender, key=key, purpose=SmsPurpose.PHONE_VERIFICATION,
            to=PHONE, text=TEXT,
        )  # fmt: skip
    assert (delivery.status, delivery.first_attempt) == (SmsStatus.SENT, False)
    assert len(sender.sent) == 1
    assert len(await _rows(committed_sessionmaker)) == 1


@pytest.mark.parametrize("kind", ["unavailable", "rejected", "timeout"])
async def test_a_failed_send_is_recorded_and_never_retried(
    committed_sessionmaker: async_sessionmaker[AsyncSession], kind: SmsFailure
) -> None:
    """No automatic retry: a POST that timed out may have gone through, and the person
    would get two codes. They ask for a new code instead (a new key)."""
    sender = FailingSender(SmsSendError(kind))
    key = _key()
    for attempt in range(2):
        delivery = await send_once(
            committed_sessionmaker, sender, key=key, purpose=SmsPurpose.PHONE_VERIFICATION,
            to=PHONE, text=TEXT,
        )  # fmt: skip
        assert (delivery.status, delivery.first_attempt) == (SmsStatus.FAILED, attempt == 0)
    assert sender.calls == 1
    (row,) = await _rows(committed_sessionmaker)
    assert (row.status, row.error) == (SmsStatus.FAILED, kind)


async def test_an_unexpected_error_is_recorded_then_raised(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A bug in a sender still leaves the row settled (never stuck in `sending`)."""
    sender = FailingSender(RuntimeError("a bug"))
    with pytest.raises(RuntimeError, match="a bug"):
        await send_once(
            committed_sessionmaker, sender, key=_key(), purpose=SmsPurpose.PHONE_VERIFICATION,
            to=PHONE, text=TEXT,
        )  # fmt: skip
    (row,) = await _rows(committed_sessionmaker)
    assert (row.status, row.error) == (SmsStatus.FAILED, "error")


async def test_two_requests_at_once_send_one_sms(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The unique key decides, not a check-then-insert: a double-click sends one code."""
    release = asyncio.Event()

    class SlowSender(FakeSmsSender):
        async def send(self, to: str, text: str) -> str:
            await release.wait()
            return await super().send(to, text)

    sender = SlowSender()
    key = _key()

    async def attempt() -> object:
        return await send_once(
            committed_sessionmaker, sender, key=key, purpose=SmsPurpose.PHONE_VERIFICATION,
            to=PHONE, text=TEXT,
        )  # fmt: skip

    first = asyncio.create_task(attempt())
    second = asyncio.create_task(attempt())
    await asyncio.sleep(0.2)  # both have tried to claim the key
    release.set()
    deliveries = await asyncio.gather(first, second)
    assert sorted(d.first_attempt for d in deliveries) == [False, True]  # type: ignore[attr-defined]
    assert len(sender.sent) == 1


@pytest.mark.parametrize(
    ("to", "key"),
    [
        ("0911234567", "otp:1"),  # not E.164
        ("+251811234567", "otp:1"),  # not a mobile
        (PHONE, ""),
        (PHONE, " otp:1"),
        (PHONE, "x" * 101),
    ],
)
async def test_bad_arguments_are_a_programming_error(
    committed_sessionmaker: async_sessionmaker[AsyncSession], to: str, key: str
) -> None:
    sender = FakeSmsSender()
    with pytest.raises(ValueError, match="must be"):
        await send_once(
            committed_sessionmaker, sender, key=key, purpose=SmsPurpose.PHONE_VERIFICATION,
            to=to, text=TEXT,
        )  # fmt: skip
    assert sender.sent == []
    assert await _rows(committed_sessionmaker) == []


async def test_the_database_down_sends_nothing(
    unreachable_sessions: AsyncSession,
) -> None:
    """Without the claim there is no de-duplication: nothing goes out."""
    sender = FakeSmsSender()

    def sessions() -> AsyncSession:
        return unreachable_sessions

    with pytest.raises(Exception):  # noqa: B017, PT011 - the driver's connection error
        await send_once(
            sessions,  # type: ignore[arg-type]
            sender, key=_key(), purpose=SmsPurpose.PHONE_VERIFICATION, to=PHONE, text=TEXT,
        )  # fmt: skip
    assert sender.sent == []
