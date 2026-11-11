"""GZ-030: sending an SMS once per event, inline (the free plan has no worker).

`send_once` claims the event's key in `gize.sms_messages`, sends outside any transaction,
then records the outcome. The key makes a retried request harmless: the SMS goes out at
most once, whatever happened the first time. The text never reaches the table (it holds a
code). Runs on committed rows: the claim must hold across connections.
"""

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, get_args

import pytest
import structlog
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.db import OpenTransactionError, is_database_unavailable
from gize_api.core.telemetry import Telemetry, configure_telemetry
from gize_api.integrations.sms import FakeSmsSender, SmsFailure, SmsSender, SmsSendError
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from gize_api.sms.service import SmsDelivery, send_once
from tests.support import SettingsFactory

PHONE = "+251911234567"
TEXT = "Your Gize code is 123456"


def _key() -> str:
    return f"otp:{uuid.uuid4()}"


async def _send(
    sessions: Callable[[], AsyncSession],
    sender: SmsSender,
    key: str,
    *,
    to: str = PHONE,
    telemetry: Telemetry | None = None,
) -> SmsDelivery:
    return await send_once(
        sessions,
        sender,
        key=key,
        purpose=SmsPurpose.PHONE_VERIFICATION,
        to=to,
        text=TEXT,
        telemetry=telemetry,
    )


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[SmsMessage]:
    async with sessions() as session, session.begin():
        return list((await session.scalars(select(SmsMessage))).all())


class FailingSender:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def send(self, to: str, text: str) -> str:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None


async def test_it_sends_and_records_the_outcome_without_the_text(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSmsSender()
    key = _key()
    delivery = await _send(committed_sessionmaker, sender, key)
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
        delivery = await _send(committed_sessionmaker, sender, key)
    assert (delivery.status, delivery.first_attempt) == (SmsStatus.SENT, False)
    assert len(sender.sent) == 1
    assert len(await _rows(committed_sessionmaker)) == 1


@pytest.mark.parametrize("kind", get_args(SmsFailure))
async def test_a_failed_send_is_recorded_and_never_retried(
    committed_sessionmaker: async_sessionmaker[AsyncSession], kind: SmsFailure
) -> None:
    """No automatic retry: a POST that timed out may have gone through, and the person
    would get two codes. They ask for a new code instead (a new key)."""
    sender = FailingSender(SmsSendError(kind))
    key = _key()
    with structlog.testing.capture_logs() as logs:
        for attempt in range(2):
            delivery = await _send(committed_sessionmaker, sender, key)
            assert (delivery.status, delivery.first_attempt) == (SmsStatus.FAILED, attempt == 0)
    assert sender.calls == 1
    (row,) = await _rows(committed_sessionmaker)
    assert (row.status, row.error) == (SmsStatus.FAILED, kind)
    _assert_one_failure_logged(logs, kind)


def _assert_one_failure_logged(logs: Sequence[Mapping[str, Any]], kind: str) -> None:
    """GZ-070: GizeApiSmsFailing counts `sms_failed` lines, so every way a send fails must
    write exactly one: a warning, so a missing provider shows; never the phone or the text."""
    (warning,) = [log for log in logs if log["event"] == "sms_failed"]
    assert warning["log_level"] == "warning"
    assert (warning["purpose"], warning["error"]) == ("phone_verification", kind)
    assert PHONE not in repr(logs)
    assert TEXT not in repr(logs)


async def test_an_unexpected_error_is_recorded_then_raised(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A bug in a sender still leaves the row settled (never stuck in `sending`)."""
    sender = FailingSender(RuntimeError("a bug"))
    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError, match="a bug"):
        await _send(committed_sessionmaker, sender, _key())
    (row,) = await _rows(committed_sessionmaker)
    assert (row.status, row.error) == (SmsStatus.FAILED, "error")
    _assert_one_failure_logged(logs, "error")


async def test_a_send_cancelled_midway_is_recorded_as_a_timeout(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Shutdown or a client gone: it may have gone out, so it's settled, then cancelled."""
    entered = asyncio.Event()

    class HangingSender:
        async def send(self, to: str, text: str) -> str:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("never answers")  # pragma: no cover

        async def aclose(self) -> None:
            return None

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(_send(committed_sessionmaker, HangingSender(), _key()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    (row,) = await _rows(committed_sessionmaker)
    assert (row.status, row.error) == (SmsStatus.FAILED, "timeout")
    _assert_one_failure_logged(logs, "timeout")


async def test_recording_a_sent_sms_can_fail_and_it_still_goes_out_once(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    unreachable_sessions: AsyncSession,
) -> None:
    """The database fails between the send and its record: the caller gets the error, the
    row stays `sending`, and the same key never sends again (the person asks anew)."""
    calls = 0

    def sessions() -> AsyncSession:
        nonlocal calls
        calls += 1
        return unreachable_sessions if calls == 2 else committed_sessionmaker()

    sender = FakeSmsSender()
    key = _key()
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - checked just below
        await _send(sessions, sender, key)
    assert is_database_unavailable(exc_info.value)
    again = await _send(committed_sessionmaker, sender, key)
    assert (again.status, again.first_attempt) == (SmsStatus.SENDING, False)
    assert len(sender.sent) == 1


async def test_two_requests_at_once_send_one_sms(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The unique key decides, not a check-then-insert: a double-click sends one code."""
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowSender(FakeSmsSender):
        async def send(self, to: str, text: str) -> str:
            entered.set()
            await release.wait()
            return await super().send(to, text)

    sender = SlowSender()
    key = _key()
    first = asyncio.create_task(_send(committed_sessionmaker, sender, key))
    await asyncio.wait_for(entered.wait(), timeout=5)  # the first holds the key, mid-send
    second = await _send(committed_sessionmaker, sender, key)  # finds it, sends nothing
    assert (second.status, second.first_attempt) == (SmsStatus.SENDING, False)
    release.set()
    assert (await first).status is SmsStatus.SENT
    assert len(sender.sent) == 1


async def test_it_refuses_to_run_inside_a_transaction(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The send must not hold a database transaction open (idle-in-transaction timeout)."""
    sender = FakeSmsSender()
    async with committed_sessionmaker() as session, session.begin():
        with pytest.raises(OpenTransactionError):
            await _send(committed_sessionmaker, sender, _key())
    assert sender.sent == []
    assert await _rows(committed_sessionmaker) == []


async def test_every_call_is_measured_without_the_phone(
    committed_sessionmaker: async_sessionmaker[AsyncSession], make_settings: SettingsFactory
) -> None:
    metrics = InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    key = _key()
    await _send(committed_sessionmaker, FakeSmsSender(), key, telemetry=telemetry)
    await _send(committed_sessionmaker, FakeSmsSender(), key, telemetry=telemetry)
    failing = FailingSender(SmsSendError("unavailable"))
    await _send(committed_sessionmaker, failing, _key(), telemetry=telemetry)
    data = metrics.get_metrics_data()
    assert data is not None
    points: list[dict[str, Any]] = [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "gize.sms.send.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    ]
    assert sorted(point["outcome"] for point in points) == ["duplicate", "sent", "unavailable"]
    assert {point["purpose"] for point in points} == {"phone_verification"}
    assert PHONE not in repr(points)


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
        await _send(committed_sessionmaker, sender, key, to=to)
    assert sender.sent == []
    assert await _rows(committed_sessionmaker) == []


async def test_the_database_down_sends_nothing(unreachable_sessions: AsyncSession) -> None:
    """Without the claim there is no de-duplication: nothing goes out."""
    sender = FakeSmsSender()
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - checked just below
        await _send(lambda: unreachable_sessions, sender, _key())
    assert is_database_unavailable(exc_info.value)
    assert sender.sent == []
