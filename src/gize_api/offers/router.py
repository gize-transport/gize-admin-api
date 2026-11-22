"""Seat offers (GZ-040): staff offer a route's free seat to the oldest request and accept or
withdraw it; the asker may accept on the website with their verified phone."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.ratelimit import PUBLIC_SIGNUP, rate_limited
from gize_api.core.security import CurrentStaff, current_staff, staff_router
from gize_api.offers import service
from gize_api.offers.schemas import (
    OfferCreate,
    OfferListParams,
    OfferPage,
    OfferQueuePage,
    OfferQueueParams,
    OfferRead,
    OpenOfferListParams,
    PublicOfferAccept,
    PublicOfferAccepted,
)
from gize_api.verification.router import token_key
from gize_api.verification.service import prove
from gize_api.verification.tokens import phone_scope

router = staff_router(tags=["offers"])
public_router = APIRouter(prefix="/v1/public/offers", tags=["public"])

_KEYED = (
    ErrorCode.IDEMPOTENCY_IN_PROGRESS,
    ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
)


def offer_list_params(params: Annotated[OfferListParams, Query()]) -> OfferListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "/v1/routes/{route_id}/offers",
    operation_id="offers_create",
    summary="Offer a route's free seat",
    status_code=201,
    response_model=OfferRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new offer.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.NOT_FOUND,
            ErrorCode.ROUTE_NOT_READY,
            ErrorCode.ROUTE_FULL,
            ErrorCode.NO_WAITING_REQUESTS,
            ErrorCode.ROUTE_HAS_WAITING_REQUESTS,
            ErrorCode.OFFER_OPEN,
            *_KEYED,
        ),
    },
)
async def create(
    route_id: uuid.UUID,
    body: OfferCreate,
    request: Request,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. Offers the seat for 48 hours (it holds it meanwhile) to
    the oldest adult request waiting for the route (asked for it, or matched to it), or to
    `request_id` (ahead of older ones: a `skip_reason`, audited). The person gets an SMS
    after the answer (`sms_status` on the offer; a failure keeps it: call them).

    - 409 ROUTE_NOT_READY: not a forming or active public route.
    - 409 ROUTE_FULL: no seat free from the start (riders and open offers counted).
    - 409 NO_WAITING_REQUESTS: nobody waits for the route.
    - 409 ROUTE_HAS_WAITING_REQUESTS: `request_id` passes older requests: send a reason.
    - 409 OFFER_OPEN: that request has an open offer.
    - 422 on `request_id`: not waiting for this route; on `start_date`: not a coming 1st.
    """
    response = await service.create(
        session,
        caller,
        route_id,
        body,
        idempotent,
        sender=request.app.state.sms_sender,
        telemetry=request.app.telemetry,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/v1/routes/{route_id}/offers",
    operation_id="offers_list",
    summary="A route's offers, newest first",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.INVALID_CURSOR),
)
async def list_offers(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: OfferListParams = Depends(offer_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OfferPage:
    """Every offer the route made, whatever its status (`expired` once its 48 hours pass),
    newest first. 404 NOT_FOUND for a route that never existed or was deleted."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_offers(session, route_id, params)


def offer_queue_params(params: Annotated[OfferQueueParams, Query()]) -> OfferQueueParams:
    _ = params.queue_after  # a bad cursor is refused before any work
    return params


@router.get(
    "/v1/routes/{route_id}/offer-queue",
    operation_id="offers_queue",
    summary="Who a route's free seat goes to, in order",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.INVALID_CURSOR),
)
async def offer_queue(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: OfferQueueParams = Depends(offer_queue_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OfferQueuePage:
    """The open requests waiting for the route (asked for it, or matched to it), in the
    order `offers_create` picks: `position` 1 is whom it offers the seat to by default.
    Oldest first, but requests offered a seat on this route before (`offered_before`: it
    expired or was withdrawn) after the others. Then those that can't be offered a seat,
    oldest first, with the reason (`not_offerable`): an open offer on any route, or a
    student's request. Positions continue from page to page, as of each page's read. Any
    route, even one that can't be offered from (only forming and active public ones can)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.queue(session, route_id, params)


def open_offer_params(params: Annotated[OpenOfferListParams, Query()]) -> OpenOfferListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.get(
    "/v1/offers",
    operation_id="offers_list_open",
    summary="Open seat offers on every route, soonest to run out first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_open(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: OpenOfferListParams = Depends(open_offer_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OfferPage:
    """The offers the overview counts as open (GZ-084): open, within their 48 hours, their
    request still waiting for the route. The one running out soonest first (then by id),
    so staff phone that person first while text messages are paused. Each item is a
    route's offer list's item: no phone numbers (`request_id` opens the request)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_open(session, params)


@router.get(
    "/v1/offers/{offer_id}",
    operation_id="offers_get",
    summary="One offer",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    offer_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OfferRead:
    """`expired` once its 48 hours pass, whether or not anything ran since."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, offer_id)


@router.post(
    "/v1/offers/{offer_id}/accept",
    operation_id="offers_accept",
    summary="Accept an offer for the person (by phone)",
    response_model=OfferRead,
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.OFFER_CLOSED,
        ErrorCode.RIDER_EXISTS,
        ErrorCode.RIDER_HAS_SEAT,
        ErrorCode.ROUTE_FULL,
        *_KEYED,
    ),
)
async def accept(
    offer_id: uuid.UUID,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. The seat becomes theirs: a rider is made from the request
    if they have none (A to B, round trip; edit it after), a place from the offer's 1st
    (a reservation on a forming route); the request is fulfilled.

    - 409 OFFER_CLOSED: accepted, withdrawn, or past its 48 hours.
    - 409 RIDER_HAS_SEAT: they ride another route: move them instead (`PUT /v1/riders/{id}/seat`).
    - 409 RIDER_EXISTS: another adult rider has the request's phone (staff sort out who is
      who).
    """
    response = await service.accept_by_staff(session, caller, offer_id, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/v1/offers/{offer_id}/withdraw",
    operation_id="offers_withdraw",
    summary="Withdraw an offer",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.OFFER_CLOSED),
)
async def withdraw(
    offer_id: uuid.UUID,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OfferRead:
    """The person said no (or the offer was a mistake): the seat is free for the next
    request. 409 OFFER_CLOSED when it isn't open."""
    response.headers["Cache-Control"] = "no-store"
    return await service.withdraw(session, caller, offer_id)


@public_router.post(
    "/{offer_id}/accept",
    operation_id="public_offers_accept",
    summary="Accept your offer (website)",
    response_model=PublicOfferAccepted,
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.OFFER_CLOSED,
        ErrorCode.RIDER_EXISTS,
        ErrorCode.RIDER_HAS_SEAT,
        ErrorCode.ROUTE_FULL,
        ErrorCode.PHONE_NOT_VERIFIED,
        ErrorCode.RATE_LIMITED,
        ErrorCode.SERVICE_UNAVAILABLE,
        *_KEYED,
    ),
    # The key first: switched off, nothing is counted against the limits.
    dependencies=[Depends(token_key), Depends(rate_limited(PUBLIC_SIGNUP))],
)
async def accept_on_website(
    offer_id: uuid.UUID,
    body: PublicOfferAccept,
    request: Request,
    key: str = Depends(token_key),
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Public: the phone the request gave, verified (GZ-031; the token is spent). Requires
    `Idempotency-Key`. 404 for an offer that isn't theirs, as for one that doesn't exist;
    409 RIDER_EXISTS when another rider has their phone (call Gize). Limits: 5 an hour per
    caller, 3 a day per phone."""
    # Signed by us, expired or not: enough to find an acceptance this retries.
    proved = prove(body.verified_phone_token, key, check_expiry=False)
    response = await service.accept_on_website(
        session,
        offer_id,
        proved,
        scope=phone_scope(proved.phone, key=key),
        idempotent=idempotent,
        limiter=request.app.state.rate_limiter,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
