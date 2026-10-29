"""/v1/audit-log: who changed what (GZ-012). Admins only."""

from typing import Annotated

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.audit_log import service
from gize_api.audit_log.schemas import AuditEntryRead, AuditLogParams
from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.pagination import Page
from gize_api.core.security import CurrentStaff, require_role, staff_router
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/audit-log", tags=["audit log"])


def audit_log_params(params: Annotated[AuditLogParams, Query()]) -> AuditLogParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.get(
    "",
    operation_id="audit_log_list",
    summary="The audit log, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def audit_log(
    response: Response,
    _: CurrentStaff = Depends(require_role(StaffRole.ADMIN)),  # noqa: B008
    params: AuditLogParams = Depends(audit_log_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Page[AuditEntryRead]:
    """Every recorded change, filtered by entity or actor. Names, emails and other personal
    values never appear: the entries show only that they changed."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_entries(session, params)
