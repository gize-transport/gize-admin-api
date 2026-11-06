"""/v1/drivers: who drives the vans (GZ-050). Staff add, list, read and edit them (and
deactivate one); admins delete. Phones never go in URLs."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.drivers import service
from gize_api.drivers.schemas import (
    DriverCreate,
    DriverListParams,
    DriverPage,
    DriverRead,
    DriverUpdate,
)
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/drivers", tags=["drivers"])

require_admin = require_role(StaffRole.ADMIN)


def driver_list_params(params: Annotated[DriverListParams, Query()]) -> DriverListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="drivers_create",
    summary="Add a driver",
    status_code=201,
    response_model=DriverRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new driver.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.DRIVER_EXISTS,
            ErrorCode.PLATE_TAKEN,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    driver: DriverCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. 409 DRIVER_EXISTS: a live driver has this phone; 409
    PLATE_TAKEN: one has this plate."""
    response = await service.create(session, caller, driver, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="drivers_list",
    summary="Drivers, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_drivers(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: DriverListParams = Depends(driver_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> DriverPage:
    """`q` finds names, or plates (any case or spacing), starting with it; `is_active`
    filters. Deleted drivers are not listed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_drivers(session, params)


@router.get(
    "/{driver_id}",
    operation_id="drivers_get",
    summary="One driver",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    driver_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> DriverRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, driver_id)


@router.patch(
    "/{driver_id}",
    operation_id="drivers_update",
    summary="Change a driver, or deactivate them",
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.DRIVER_EXISTS,
        ErrorCode.PLATE_TAKEN,
        ErrorCode.DRIVER_ASSIGNED,
    ),
)
async def update(
    driver_id: uuid.UUID,
    change: DriverUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> DriverRead:
    """Leave out what stays. Setting what is there changes nothing. 409 DRIVER_ASSIGNED:
    deactivating them while they drive a route (take them off it first)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, driver_id, change)


@router.delete(
    "/{driver_id}",
    operation_id="drivers_delete",
    summary="Delete a driver (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.DRIVER_ASSIGNED),
)
async def delete(
    driver_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: hidden from lists, their phone and plate free again. 409
    DRIVER_ASSIGNED while they drive a route."""
    await service.delete(session, caller, driver_id)
    return Response(status_code=204)
