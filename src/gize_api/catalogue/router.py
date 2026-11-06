"""/v1/public: what the rider app reads before a sign-up (GZ-036). No sign-in: these are
public, rate limited per caller (`PUBLIC_READ`, failing open: reads only, nothing personal)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.catalogue import service
from gize_api.catalogue.schemas import (
    PublicAreaListParams,
    PublicAreaPage,
    PublicRouteListParams,
    PublicRoutePage,
)
from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.ratelimit import PUBLIC_READ, rate_limited

router = APIRouter(
    prefix="/v1/public", tags=["public"], dependencies=[Depends(rate_limited(PUBLIC_READ))]
)


def area_list_params(params: Annotated[PublicAreaListParams, Query()]) -> PublicAreaListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


def route_list_params(
    params: Annotated[PublicRouteListParams, Query()],
) -> PublicRouteListParams:
    _ = params.after
    return params


@router.get(
    "/areas",
    operation_id="public_areas_list",
    summary="Areas to choose from",
    responses=problem_responses(
        ErrorCode.INVALID_CURSOR, ErrorCode.RATE_LIMITED, ErrorCode.SERVICE_UNAVAILABLE
    ),
)
async def list_areas(
    response: Response,
    params: PublicAreaListParams = Depends(area_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PublicAreaPage:
    """Oldest first. `q` finds names starting with it, ignoring case. 60 reads a minute
    per caller, shared with `/v1/public/routes`."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_areas(session, params)


@router.get(
    "/routes",
    operation_id="public_routes_list",
    summary="Public routes for a trip",
    responses=problem_responses(
        ErrorCode.INVALID_CURSOR, ErrorCode.RATE_LIMITED, ErrorCode.SERVICE_UNAVAILABLE
    ),
)
async def list_routes(
    response: Response,
    params: PublicRouteListParams = Depends(route_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PublicRoutePage:
    """Public routes taking a rider from `from_area_id` (A, or an area the route serves) to
    `to_area_id` (B), oldest first: `forming` ones take reservations, `active` and `paused`
    ones requests. Unknown areas list none. 60 reads a minute per caller, shared with
    `/v1/public/areas`."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_routes(session, params)
