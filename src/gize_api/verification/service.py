"""Phone verification (GZ-031): send a code, check it, hand back a verified-phone token.

Requesting a code (Idempotency-Key; the SMS limits: 10/hour per caller, 3/hour per phone,
failing closed):
1. in one transaction, claim the key, store the verification (the code only as a hash,
   valid 10 minutes) and record the response;
2. send the SMS (GZ-030's `send_once`, keyed by the verification), with no transaction open;
3. if it didn't go out, release the key and expire the verification, then answer 503
   SMS_UNAVAILABLE: the app's retry with the same key runs again instead of replaying a
   success. A send left `sending` counts as not sent, and so does anything that stops the
   send (the database failing, a bug, the request cancelled): the key is released and the
   code expired before the error goes on, so a stored "sent" never outlives an SMS that
   wasn't. A `timeout` (sent, but no answer from the gateway, GZ-038) counts as sent: the
   code may arrive, so it stays valid; if it doesn't, the person asks again (a new code).

Every request counts against the limits, replays included (a retried success uses one of
the phone's three codes an hour): the safe direction.

Checking a code locks the verification, so parallel guesses can't exceed five attempts. A
wrong code is counted (and committed) before the answer; an expired, used, exhausted or
unknown verification gets one answer, 410 VERIFICATION_EXPIRED, so nothing tells a guesser
which IDs exist.
"""

import asyncio
import hmac
import secrets
import uuid
from datetime import timedelta

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import idempotency
from gize_api.core.db import borrowed
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.ratelimit import SMS, RateLimiter
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.sms import SmsSender
from gize_api.sms.models import SmsPurpose
from gize_api.sms.service import send_once
from gize_api.verification.models import MAX_ATTEMPTS, PhoneVerification
from gize_api.verification.schemas import (
    CODE_LENGTH,
    PhoneVerified,
    VerificationCheck,
    VerificationSent,
)
from gize_api.verification.tokens import (
    InvalidVerifiedPhoneTokenError,
    VerifiedPhone,
    code_hash,
    issue_verified_phone_token,
    phone_scope,
    read_verified_phone_token,
)

CODE_LIFETIME = timedelta(minutes=10)
SMS_RETRY_AFTER_SECONDS = "60"


def message(code: str) -> str:
    return f"Gize: your code is {code}. It expires in 10 minutes."


async def request_code(
    session: AsyncSession,
    *,
    phone: str,
    idempotent: IdempotentRequest,
    key: str,
    limiter: RateLimiter,
    sender: SmsSender,
    telemetry: Telemetry | None,
) -> Response:
    await limiter.check(SMS, phone=phone)  # the caller's IP is counted by the route
    scope = phone_scope(phone, key=key)
    verification_id = uuid.uuid4()
    code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
    async with session.begin():
        claimed = await claim(session, scope=scope, request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        verification = PhoneVerification(
            id=verification_id,
            phone=phone,
            code_hash=code_hash(verification_id, code, key=key),
            expires_at=func.now() + CODE_LIFETIME,
        )
        session.add(verification)
        await session.flush()
        await session.refresh(verification, ["expires_at"])
        payload = VerificationSent(
            verification_id=verification_id, expires_at=verification.expires_at
        ).model_dump(mode="json")
        await record(session, claimed, status=201, body=payload)

    try:
        delivery = await send_once(
            borrowed(session),
            sender,
            key=f"otp:{verification_id}",
            purpose=SmsPurpose.PHONE_VERIFICATION,
            to=phone,
            text=message(code),
            telemetry=telemetry,
        )
    except BaseException:
        # Not known to be sent: forget the success, even while being cancelled.
        await asyncio.shield(_withdraw(session, scope, idempotent, verification_id))
        raise
    if not delivery.maybe_sent:
        await _withdraw(session, scope, idempotent, verification_id)
        raise AppError(ErrorCode.SMS_UNAVAILABLE, headers={"Retry-After": SMS_RETRY_AFTER_SECONDS})
    return JSONResponse(payload, status_code=201)


async def _withdraw(
    session: AsyncSession,
    scope: str,
    idempotent: IdempotentRequest,
    verification_id: uuid.UUID,
) -> None:
    """Release the key (a retry runs again) and expire the code (never usable)."""
    async with session.begin():
        await idempotency.release(session, scope=scope, key=idempotent.key)
        await session.execute(
            update(PhoneVerification)
            .where(PhoneVerification.id == verification_id)
            .values(expires_at=PhoneVerification.created_at)
        )


async def check_code(session: AsyncSession, check: VerificationCheck, *, key: str) -> PhoneVerified:
    async with session.begin():
        found = (
            await session.execute(
                select(PhoneVerification, func.now())
                .where(PhoneVerification.id == check.verification_id)
                .with_for_update()
            )
        ).one_or_none()
        if found is None:
            raise AppError(ErrorCode.VERIFICATION_EXPIRED)
        verification, now = found
        if (
            verification.verified_at is not None
            or verification.attempts >= MAX_ATTEMPTS
            or verification.expires_at <= now
        ):
            raise AppError(ErrorCode.VERIFICATION_EXPIRED)
        expected = code_hash(verification.id, check.code, key=key)
        right = hmac.compare_digest(expected, verification.code_hash)
        if right:
            verification.verified_at = now
            token, expires_at = issue_verified_phone_token(
                verification.phone, verification.id, key=key
            )
        else:
            verification.attempts += 1  # committed before the answer below
    if not right:
        raise AppError(ErrorCode.VERIFICATION_CODE_INVALID)
    return PhoneVerified(verified_phone_token=token, expires_at=expires_at)


def prove(token: str, key: str, *, check_expiry: bool = True) -> VerifiedPhone:
    """The phone a verified-phone token proves (GZ-032, GZ-040); 401 PHONE_NOT_VERIFIED
    otherwise. `check_expiry=False` only to find which earlier request a retry repeats."""
    try:
        return read_verified_phone_token(token, key=key, check_expiry=check_expiry)
    except InvalidVerifiedPhoneTokenError:
        raise AppError(ErrorCode.PHONE_NOT_VERIFIED) from None


async def spend(session: AsyncSession, proved: VerifiedPhone) -> None:
    """The token's verification, confirmed for this phone and not spent yet, is spent now
    (locked: two uses of one token, one gets through); 401 PHONE_NOT_VERIFIED otherwise."""
    verification = await session.scalar(
        select(PhoneVerification)
        .where(PhoneVerification.id == proved.verification_id)
        .with_for_update()
    )
    if (
        verification is None
        or verification.phone != proved.phone
        or verification.verified_at is None
        or verification.consumed_at is not None
    ):
        raise AppError(ErrorCode.PHONE_NOT_VERIFIED)
    verification.consumed_at = func.now()
