"""/v1/routes: routes, created, listed, read and edited by staff (GZ-021). Status, prices
and deletion have their own endpoints (GZ-024, GZ-022)."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, staff_router
from gize_api.routes import service
from gize_api.routes.schemas import (
    RouteCreate,
    RouteListParams,
    RoutePage,
    RouteRead,
    RouteUpdate,
)

router = staff_router(prefix="/v1/routes", tags=["routes"])


def route_list_params(params: Annotated[RouteListParams, Query()]) -> RouteListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="routes_create",
    summary="Add a route",
    status_code=201,
    response_model=RouteRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new route.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.ROUTE_CODE_TAKEN,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    route: RouteCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """A `draft` route. Requires `Idempotency-Key`. 409 ROUTE_CODE_TAKEN: a live route has
    this code. A corridor or company that doesn't exist (or a company that isn't active) is
    a 422 on its field."""
    response = await service.create(session, caller, route, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="routes_list",
    summary="Routes, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_routes(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: RouteListParams = Depends(route_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RoutePage:
    """With corridor, areas, company, and seats taken and free this month and next."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_routes(session, params)


@router.get(
    "/{route_id}",
    operation_id="routes_get",
    summary="One route",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, route_id)


@router.patch(
    "/{route_id}",
    operation_id="routes_update",
    summary="Change a route",
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.ROUTE_CODE_TAKEN,
        ErrorCode.ROUTE_HAS_RIDERS,
        ErrorCode.SEATS_BELOW_LAUNCH_MINIMUM,
        ErrorCode.SEATS_BELOW_TAKEN,
    ),
)
async def update(
    route_id: uuid.UUID,
    change: RouteUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """Leave out what stays. The type is fixed.

    - 409 ROUTE_HAS_RIDERS: corridor, school or company while riders hold seats.
    - 409 SEATS_BELOW_TAKEN: fewer seats than are taken on some day from today.
    - 409 SEATS_BELOW_LAUNCH_MINIMUM: the launch minimum above the seats.
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, route_id, change)
