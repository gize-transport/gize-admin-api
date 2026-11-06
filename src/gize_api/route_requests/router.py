"""/v1/route-requests: unmet demand from individual riders, recorded, listed, read,
corrected and moved by staff; deleted by admins (GZ-027)."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.route_requests import service
from gize_api.route_requests.schemas import (
    RouteRequestCreate,
    RouteRequestListParams,
    RouteRequestPage,
    RouteRequestRead,
    RouteRequestStatusSet,
    RouteRequestSummary,
    RouteRequestUpdate,
)
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/route-requests", tags=["route-requests"])

require_admin = require_role(StaffRole.ADMIN)


def request_list_params(
    params: Annotated[RouteRequestListParams, Query()],
) -> RouteRequestListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="route_requests_create",
    summary="Record a route request",
    status_code=201,
    response_model=RouteRequestRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new request.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.ROUTE_REQUEST_EXISTS,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    request: RouteRequestCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """A `new` request from `staff`. Requires `Idempotency-Key`. 409 ROUTE_REQUEST_EXISTS:
    the same rider already has an open request for this. An area or route that doesn't exist
    or doesn't fit is a 422 on its field."""
    response = await service.create(session, caller, request, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="route_requests_list",
    summary="Route requests, oldest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_requests(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: RouteRequestListParams = Depends(request_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRequestPage:
    """Oldest first: the order free seats are offered in."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_requests(session, params)


@router.get(
    "/summary",
    operation_id="route_requests_summary",
    summary="Open demand per area pair and per route",
)
async def summary(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRequestSummary:
    """Where the next route or van should go: open new-areas requests not matched yet per
    area pair, and open requests per route (matched to it, else asking for it) next to its
    free seats next month. Busiest first, at most 100 of each."""
    response.headers["Cache-Control"] = "no-store"
    return await service.summary(session)


@router.get(
    "/{request_id}",
    operation_id="route_requests_get",
    summary="One route request",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    request_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRequestRead:
    """A live route request by ID, whatever its status. 404 NOT_FOUND for one that never
    existed or was deleted."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, request_id)


@router.patch(
    "/{request_id}",
    operation_id="route_requests_update",
    summary="Correct a route request",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.ROUTE_REQUEST_EXISTS),
)
async def update(
    request_id: uuid.UUID,
    change: RouteRequestUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRequestRead:
    """Who and when; what is asked for doesn't change. 409 ROUTE_REQUEST_EXISTS: the change
    would make it the same as another open request."""
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, request_id, change)


@router.put(
    "/{request_id}/status",
    operation_id="route_requests_set_status",
    summary="Match, fulfil or decline a route request",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.INVALID_STATUS_CHANGE),
)
async def set_status(
    request_id: uuid.UUID,
    change: RouteRequestStatusSet,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRequestRead:
    """`new` → `matched` (`matched_route_id`) → `fulfilled`; `new` → `fulfilled`; `new` or
    `matched` → `declined` (`decline_reason`). A matched request may be matched to another
    route. The status it has changes nothing.

    - 409 INVALID_STATUS_CHANGE: any other move (fulfilled and declined are final).
    - 422 on `matched_route_id`: not a live route for this rider (public for adults;
      student, of their school, for students).
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.set_status(session, caller, request_id, change)


@router.delete(
    "/{request_id}",
    operation_id="route_requests_delete",
    summary="Delete a route request (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def delete(
    request_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete, for a mistaken record: hidden from lists and counts; history keeps it."""
    await service.delete(session, caller, request_id)
    return Response(status_code=204)
