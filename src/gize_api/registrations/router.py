"""/v1/registrations: public sign-ups from the rider app (GZ-032). No sign-in: a verified
phone instead (GZ-031), and the `PUBLIC_SIGNUP` limits, failing closed (503) without
Redis."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.ratelimit import PUBLIC_SIGNUP, rate_limited
from gize_api.registrations import service
from gize_api.registrations.schemas import AdultRegistered, AdultRegistration
from gize_api.verification.router import token_key

router = APIRouter(prefix="/v1/registrations", tags=["registrations"])


@router.post(
    "/adult",
    operation_id="registrations_adult",
    summary="An adult signs up",
    status_code=201,
    response_model=AdultRegistered,
    responses=problem_responses(
        ErrorCode.PHONE_NOT_VERIFIED,
        ErrorCode.RIDER_EXISTS,
        ErrorCode.ROUTE_FULL,
        ErrorCode.ROUTE_REQUEST_EXISTS,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.SERVICE_UNAVAILABLE,
    ),
    # The key first: switched off, nothing is counted against the limits.
    dependencies=[Depends(token_key), Depends(rate_limited(PUBLIC_SIGNUP))],
)
async def register_adult(
    body: AdultRegistration,
    request: Request,
    key: str = Depends(token_key),
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Creates the rider (the phone is the token's) and, with a `route_id`: a reservation
    on a `forming` route with a free seat, from the next 1st; a request for an `active` or
    `paused` one (never a place directly: freed seats go to the oldest request). Without
    one: a request for a route between the two areas. Requires `Idempotency-Key`; a retry
    with it replays the answer.

    - 401 PHONE_NOT_VERIFIED: the token is missing, invalid, expired or already used:
      verify the phone again.
    - 409 RIDER_EXISTS: this phone already has a rider: call Gize to change it.
    - 409 ROUTE_FULL: no seat left on the forming route; nothing was kept and the token
      still works (pick another route, or ask without one).
    - 409 ROUTE_REQUEST_EXISTS: the same request is already open.
    - 422 on `route_id`: not a route for these areas the app can sign up to; on
      `saturday`: the route has no Saturday add-on.
    - 429 RATE_LIMITED: 5 an hour per caller, 3 a day per phone.
    - 503 SERVICE_UNAVAILABLE: sign-up is switched off, or the limits can't be checked.
    """
    response = await service.register_adult(
        session,
        body,
        idempotent=idempotent,
        key=key,
        limiter=request.app.state.rate_limiter,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
