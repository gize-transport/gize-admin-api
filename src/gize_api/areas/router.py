"""/v1/areas: the list of neighbourhoods and landmarks (GZ-026). Staff manage it; admins
delete."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas import service
from gize_api.areas.schemas import AreaListParams, AreaPage, AreaRead, AreaWrite
from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/areas", tags=["areas"])

require_admin = require_role(StaffRole.ADMIN)


def area_list_params(params: Annotated[AreaListParams, Query()]) -> AreaListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="areas_create",
    summary="Add an area",
    status_code=201,
    response_model=AreaRead,
    responses={
        201: {
            "description": "Created",
            "headers": {"Location": {"description": "The new area.", "schema": {"type": "string"}}},
        },
        **problem_responses(
            ErrorCode.AREA_NAME_TAKEN,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    area: AreaWrite,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. 409 AREA_NAME_TAKEN: a live area has this name, ignoring
    case."""
    response = await service.create(session, caller, area, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="areas_list",
    summary="Areas, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_areas(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: AreaListParams = Depends(area_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AreaPage:
    """`q` finds names starting with it, ignoring case. Deleted areas are not listed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_areas(session, params)


@router.get(
    "/{area_id}",
    operation_id="areas_get",
    summary="One area",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    area_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AreaRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, area_id)


@router.patch(
    "/{area_id}",
    operation_id="areas_update",
    summary="Rename an area",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.AREA_NAME_TAKEN),
)
async def rename(
    area_id: uuid.UUID,
    change: AreaWrite,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AreaRead:
    """Everything that refers to the area shows the new name. Renaming to the current name
    changes nothing."""
    response.headers["Cache-Control"] = "no-store"
    return await service.rename(session, caller, area_id, change)


@router.delete(
    "/{area_id}",
    operation_id="areas_delete",
    summary="Delete an area (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def delete(
    area_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: the area disappears from lists and pickers, and its name can be used
    again; history keeps it."""
    await service.delete(session, caller, area_id)
    return Response(status_code=204)
