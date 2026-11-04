"""/v1/verification: phone verification for the public app (GZ-031). No sign-in: these are
the public endpoints, each rate limited per caller, failing closed (503) without Redis."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.ratelimit import SMS, VERIFICATION_CHECK, RateLimiter, rate_limited
from gize_api.verification import service
from gize_api.verification.schemas import (
    PhoneVerified,
    VerificationCheck,
    VerificationRequest,
    VerificationSent,
)

router = APIRouter(prefix="/v1/verification", tags=["verification"])


def token_key(request: Request) -> str:
    """VERIFIED_PHONE_TOKEN_KEY, or 503: no code is worth sending that can't be confirmed."""
    key = request.app.state.settings.verified_phone_token_key
    if key is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
    return str(key.get_secret_value())


@router.post(
    "/request",
    operation_id="verification_request",
    summary="Send a code to a phone",
    status_code=201,
    response_model=VerificationSent,
    responses=problem_responses(
        ErrorCode.RATE_LIMITED,
        ErrorCode.SMS_UNAVAILABLE,
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
    ),
    # The key first: switched off, nothing is counted against the limits.
    dependencies=[Depends(token_key), Depends(rate_limited(SMS))],
)
async def request_code(
    body: VerificationRequest,
    request: Request,
    key: str = Depends(token_key),
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Sends a six-digit code by SMS, valid 10 minutes, then answers with the verification
    to check it against. Requires `Idempotency-Key`: a retry with it never sends a second
    SMS, except after a 503 SMS_UNAVAILABLE (nothing went out; the retry tries again).

    - 429 RATE_LIMITED: 3 codes an hour per phone, 10 per caller.
    - 503 SMS_UNAVAILABLE: the SMS couldn't be sent (Retry-After).
    - 503 SERVICE_UNAVAILABLE: verification is switched off, or the limits can't be checked.
    """
    limiter: RateLimiter = request.app.state.rate_limiter
    response = await service.request_code(
        session,
        phone=body.phone,
        idempotent=idempotent,
        key=key,
        limiter=limiter,
        sender=request.app.state.sms_sender,
        telemetry=request.app.telemetry,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/verify",
    operation_id="verification_verify",
    summary="Check a code; get a verified-phone token",
    responses=problem_responses(
        ErrorCode.VERIFICATION_CODE_INVALID,
        ErrorCode.VERIFICATION_EXPIRED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.SERVICE_UNAVAILABLE,
    ),
    dependencies=[Depends(token_key), Depends(rate_limited(VERIFICATION_CHECK))],
)
async def verify_code(
    body: VerificationCheck,
    response: Response,
    key: str = Depends(token_key),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PhoneVerified:
    """The right code answers a token proving the phone, valid 30 minutes and good for one
    sign-up. Not idempotent: a retry after a lost answer finds the code used (410) and the
    app asks for a new one.

    - 422 VERIFICATION_CODE_INVALID: wrong code (five tries per code).
    - 410 VERIFICATION_EXPIRED: expired, already used, out of tries, or unknown: ask for a
      new code.
    - 429 RATE_LIMITED: 30 checks an hour per caller.
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.check_code(session, body, key=key)
