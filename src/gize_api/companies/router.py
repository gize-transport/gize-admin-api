"""/v1/companies: postpaid customers with their own corporate routes (GZ-029). Staff
manage them, including deactivating one; admins delete."""

import uuid
from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.companies import service
from gize_api.companies.schemas import (
    CompanyCreate,
    CompanyListParams,
    CompanyPage,
    CompanyRead,
    CompanyUpdate,
)
from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/companies", tags=["companies"])

require_admin = require_role(StaffRole.ADMIN)


def company_list_params(params: Annotated[CompanyListParams, Query()]) -> CompanyListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="companies_create",
    summary="Add a company",
    status_code=201,
    response_model=CompanyRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new company.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.COMPANY_NAME_TAKEN,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    company: CompanyCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key`. 409 COMPANY_NAME_TAKEN: a live company has this name,
    ignoring case. The contact phone is stored E.164."""
    response = await service.create(session, caller, company, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="companies_list",
    summary="Companies, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_companies(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: CompanyListParams = Depends(company_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CompanyPage:
    """`q` finds names starting with it, ignoring case; `is_active` filters. Deleted
    companies are not listed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_companies(session, params)


@router.get(
    "/{company_id}",
    operation_id="companies_get",
    summary="One company",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    company_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CompanyRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, company_id)


@router.patch(
    "/{company_id}",
    operation_id="companies_update",
    summary="Change a company, or deactivate it",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.COMPANY_NAME_TAKEN),
)
async def update(
    company_id: uuid.UUID,
    change: CompanyUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CompanyRead:
    """Leave out what stays. Empty notes clear them. Setting what is there changes nothing."""
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, company_id, change)


@router.delete(
    "/{company_id}",
    operation_id="companies_delete",
    summary="Delete a company (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def delete(
    company_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: hidden from lists, its name free again; history keeps it."""
    await service.delete(session, caller, company_id)
    return Response(status_code=204)
