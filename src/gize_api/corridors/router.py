"""/v1/corridors: pairs of areas routes run along (GZ-020). Staff manage them; admins
delete."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.corridors import service
from gize_api.corridors.schemas import (
    CorridorCreate,
    CorridorListParams,
    CorridorPage,
    CorridorRead,
    CorridorUpdate,
)
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/corridors", tags=["corridors"])

require_admin = require_role(StaffRole.ADMIN)


def corridor_list_params(params: Annotated[CorridorListParams, Query()]) -> CorridorListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="corridors_create",
    summary="Add a corridor",
    status_code=201,
    response_model=CorridorRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new corridor.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.CORRIDOR_EXISTS,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    corridor: CorridorCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. 409 CORRIDOR_EXISTS: a live corridor has this pair (the
    way back is another corridor). An area that doesn't exist is a 422 on its field."""
    response = await service.create(session, caller, corridor, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="corridors_list",
    summary="Corridors, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_corridors(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: CorridorListParams = Depends(corridor_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CorridorPage:
    """With both areas' names. Deleted corridors are not listed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_corridors(session, params)


@router.get(
    "/{corridor_id}",
    operation_id="corridors_get",
    summary="One corridor",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    corridor_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CorridorRead:
    """A live corridor by ID, with its two areas. 404 NOT_FOUND for one that never existed or
    was deleted."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, corridor_id)


@router.patch(
    "/{corridor_id}",
    operation_id="corridors_update",
    summary="Change a corridor's areas",
    responses=problem_responses(
        ErrorCode.NOT_FOUND, ErrorCode.CORRIDOR_EXISTS, ErrorCode.CORRIDOR_IN_USE
    ),
)
async def update(
    corridor_id: uuid.UUID,
    change: CorridorUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CorridorRead:
    """Leave an end out to keep it. The result must be two different areas. 409
    CORRIDOR_IN_USE: a live route runs on it (move the routes first)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, corridor_id, change)


@router.delete(
    "/{corridor_id}",
    operation_id="corridors_delete",
    summary="Delete a corridor (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.CORRIDOR_IN_USE),
)
async def delete(
    corridor_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: hidden from lists, its pair free again; history keeps it. 409
    CORRIDOR_IN_USE while a live route runs on it."""
    await service.delete(session, caller, corridor_id)
    return Response(status_code=204)
