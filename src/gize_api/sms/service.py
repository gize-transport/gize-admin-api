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
and the exception goes on. The text is never stored.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.contact import MOBILE_E164_PATTERN
from gize_api.integrations.sms import SmsSender, SmsSendError
from gize_api.sms.models import (
    DEDUP_KEY_MAX_LENGTH,
    PROVIDER_ID_MAX_LENGTH,
    SmsMessage,
    SmsPurpose,
    SmsStatus,
)


@dataclass(frozen=True)
class SmsDelivery:
    """What happened to the event's SMS. `first_attempt` is false when an earlier call had
    already handled the key: then `status` is that call's outcome and nothing was sent."""

    id: uuid.UUID
    status: SmsStatus
    first_attempt: bool


async def send_once(
    sessions: async_sessionmaker[AsyncSession],
    sender: SmsSender,
    *,
    key: str,
    purpose: SmsPurpose,
    to: str,
    text: str,
) -> SmsDelivery:
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
            return SmsDelivery(id=earlier.id, status=earlier.status, first_attempt=False)

    try:
        provider_id = await sender.send(to, text)
    except SmsSendError as exc:
        await _settle(sessions, claimed, SmsStatus.FAILED, error=exc.kind)
        return SmsDelivery(id=claimed, status=SmsStatus.FAILED, first_attempt=True)
    except Exception:
        await _settle(sessions, claimed, SmsStatus.FAILED, error="error")
        raise
    await _settle(sessions, claimed, SmsStatus.SENT, provider_id=provider_id)
    return SmsDelivery(id=claimed, status=SmsStatus.SENT, first_attempt=True)


async def _settle(
    sessions: async_sessionmaker[AsyncSession],
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
