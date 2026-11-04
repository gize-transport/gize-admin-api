"""/v1/riders: the people who ride (GZ-034). Staff record, list, look up, read and edit
them; admins delete. Phones never go in URLs: look-ups send them in the body."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.riders import service
from gize_api.riders.schemas import (
    RiderCreate,
    RiderListParams,
    RiderLookup,
    RiderMatches,
    RiderPage,
    RiderRead,
    RiderUpdate,
)
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/riders", tags=["riders"])

require_admin = require_role(StaffRole.ADMIN)


def rider_list_params(params: Annotated[RiderListParams, Query()]) -> RiderListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="riders_create",
    summary="Record an adult rider",
    status_code=201,
    response_model=RiderRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new rider.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.RIDER_EXISTS,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    rider: RiderCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. 409 RIDER_EXISTS: a live adult has this phone. Areas
    that don't exist (or are deleted) are a 422 on their field."""
    response = await service.create(session, caller, rider, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="riders_list",
    summary="Riders, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_riders(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: RiderListParams = Depends(rider_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RiderPage:
    """Each with the seat they hold (today or later) and their open requests; phones
    masked. Deleted riders are not listed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_riders(session, params)


@router.post(
    "/lookup",
    operation_id="riders_lookup",
    summary="Find riders by phone",
)
async def lookup(
    body: RiderLookup,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RiderMatches:
    """Reads only: a POST so the phone stays out of URLs and logs. Live riders with that
    phone, newest first (none: an empty list)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.lookup(session, body)


@router.get(
    "/{rider_id}",
    operation_id="riders_get",
    summary="One rider",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    rider_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RiderRead:
    """With the phone in full, the seat they hold and their open requests."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, rider_id)


@router.patch(
    "/{rider_id}",
    operation_id="riders_update",
    summary="Change a rider",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.RIDER_EXISTS),
)
async def update(
    rider_id: uuid.UUID,
    change: RiderUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RiderRead:
    """Leave out what stays. Setting what is there changes nothing.

    - 409 RIDER_EXISTS: another live adult has the new phone.
    - 422 on an area or `saturday_addon`: the rider holds a seat and the change wouldn't
      fit its route (boarding at A or an area it serves, going to B, Saturday where
      offered), or the area doesn't exist.
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, rider_id, change)


@router.delete(
    "/{rider_id}",
    operation_id="riders_delete",
    summary="Delete a rider (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.RIDER_HAS_SEAT),
)
async def delete(
    rider_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: hidden from lists, their phone free again; history keeps them. 409
    RIDER_HAS_SEAT while they hold a seat today or later: end it first."""
    await service.delete(session, caller, rider_id)
    return Response(status_code=204)
