"""Sending an SMS once per event, inline (GZ-030).

The free hosting plan has no background worker (owner decision, 2026-11-04), so the request
that needs an SMS sends it itself, in three steps:
1. claim the event's key: insert the row as `sending`, in its own transaction; a key that
   is already there means this event was handled, so nothing is sent again;
2. send, with no transaction open (`core/http.py` refuses an outbound call inside one);
3. record `sent` or `failed`, in a second transaction.

There is no automatic retry: a POST that timed out may have gone through, and a retry would
send a second code. A failure is recorded and returned; the person asks again, which is a
new event with a new key. A sender that raises something unexpected is recorded as `error`
and the exception goes on; a send cancelled midway (shutdown, a client gone) as `timeout`,
since it may have gone out. The text is never stored.

A row left `sending` (the process died mid-send, or the database failed while recording a
send that went out) is final for its key: a later call returns `sending` and sends nothing.
Callers treat `sending` like `failed`: the person asks again, with a new key, so an SMS that
did go out is never repeated for the same event.

Every call is measured (`gize.sms.send.duration`: purpose and outcome) and a failure is
logged as a warning with the purpose and kind, never the phone or the text: on staging and
production, with no provider yet, every send fails, and that must show.
"""

import asyncio
import re
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.contact import MOBILE_E164_PATTERN
from gize_api.core.db import OpenTransactionError, database_transaction_open
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.sms import SmsSender, SmsSendError
from gize_api.sms.models import (
    DEDUP_KEY_MAX_LENGTH,
    PROVIDER_ID_MAX_LENGTH,
    SmsMessage,
    SmsPurpose,
    SmsStatus,
)

logger = structlog.get_logger("gize_api.sms")


@dataclass(frozen=True)
class SmsDelivery:
    """What happened to the event's SMS. `first_attempt` is false when an earlier call had
    already handled the key: then `status` is that call's outcome and nothing was sent."""

    id: uuid.UUID
    status: SmsStatus
    first_attempt: bool


Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def send_once(
    sessions: Sessions,  # a sessionmaker, or core.db.borrowed(request session)
    sender: SmsSender,
    *,
    key: str,
    purpose: SmsPurpose,
    to: str,
    text: str,
    telemetry: Telemetry | None,
) -> SmsDelivery:
    """`telemetry` is required (pass the app's) so every call is measured."""
    if database_transaction_open():
        raise OpenTransactionError("send_once claims, sends and records in its own transactions")
    if not re.fullmatch(MOBILE_E164_PATTERN, to):
        raise ValueError("to must be an Ethiopian mobile in E.164 form")
    if not key or key != key.strip() or len(key) > DEDUP_KEY_MAX_LENGTH:
        raise ValueError(f"key must be 1 to {DEDUP_KEY_MAX_LENGTH} characters, trimmed")

    async with sessions() as session, session.begin():
        claimed = await session.scalar(
            insert(SmsMessage)
            .values(dedup_key=key, purpose=purpose, to_phone=to)
            .on_conflict_do_nothing(index_elements=[SmsMessage.dedup_key])
            .returning(SmsMessage.id)
        )
        if claimed is None:
            earlier = (
                await session.execute(
                    select(SmsMessage.id, SmsMessage.status).where(SmsMessage.dedup_key == key)
                )
            ).one()
            _measure(telemetry, purpose, "duplicate", 0.0)
            return SmsDelivery(id=earlier.id, status=earlier.status, first_attempt=False)

    started = time.monotonic()
    try:
        provider_id = await sender.send(to, text)
    except SmsSendError as exc:
        await _fail(sessions, telemetry, claimed, purpose, exc.kind, started)
        return SmsDelivery(id=claimed, status=SmsStatus.FAILED, first_attempt=True)
    except asyncio.CancelledError:
        # May have gone out: record it as a timeout, even while being cancelled.
        await asyncio.shield(_fail(sessions, telemetry, claimed, purpose, "timeout", started))
        raise
    except Exception:
        await _fail(sessions, telemetry, claimed, purpose, "error", started)
        raise
    _measure(telemetry, purpose, "sent", time.monotonic() - started)
    await _settle(sessions, claimed, SmsStatus.SENT, provider_id=provider_id)
    return SmsDelivery(id=claimed, status=SmsStatus.SENT, first_attempt=True)


async def _fail(
    sessions: Sessions,
    telemetry: Telemetry | None,
    message_id: uuid.UUID,
    purpose: SmsPurpose,
    kind: str,
    started: float,
) -> None:
    _measure(telemetry, purpose, kind, time.monotonic() - started)
    logger.warning("sms_failed", sms_id=str(message_id), purpose=purpose.value, error=kind)
    await _settle(sessions, message_id, SmsStatus.FAILED, error=kind)


def _measure(
    telemetry: Telemetry | None, purpose: SmsPurpose, outcome: str, seconds: float
) -> None:
    if telemetry is not None:
        telemetry.record_sms(purpose.value, outcome, seconds)


async def _settle(
    sessions: Sessions,
    message_id: uuid.UUID,
    status: SmsStatus,
    *,
    error: str | None = None,
    provider_id: str | None = None,
) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(SmsMessage)
            .where(SmsMessage.id == message_id)
            .values(
                status=status,
                error=error,
                provider_message_id=None
                if provider_id is None
                else provider_id[:PROVIDER_ID_MAX_LENGTH],
            )
        )
