"""/v1/staff: who is signed in (GZ-011), and staff management for admins (GZ-013)."""

import uuid
from typing import Annotated

import structlog
from fastapi import Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.ratelimit import STAFF_INVITE, rate_limited
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.integrations.supabase_auth import SupabaseAuthAdmin
from gize_api.staff import service
from gize_api.staff.models import StaffRole
from gize_api.staff.schemas import (
    StaffInvite,
    StaffListParams,
    StaffMe,
    StaffPage,
    StaffRead,
    StaffUpdate,
)

router = staff_router(prefix="/v1/staff", tags=["staff"])

# One dependency object for every use, so FastAPI resolves it once per request.
require_admin = require_role(StaffRole.ADMIN)

logger = structlog.get_logger("gize_api.staff")


def staff_list_params(params: Annotated[StaffListParams, Query()]) -> StaffListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


def auth_admin(request: Request) -> SupabaseAuthAdmin:
    """Supabase's admin API, or 503 when SUPABASE_SECRET_KEY is not set."""
    admin: SupabaseAuthAdmin | None = getattr(request.app.state, "auth_admin", None)
    if admin is None:
        logger.error("supabase_admin_not_configured", setting="SUPABASE_SECRET_KEY")
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE)
    return admin


@router.get(
    "/me",
    operation_id="staff_me",
    summary="The signed-in staff member",
)
async def me(
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaffMe:
    """Your ID, name, email and role. The admin portal calls it after sign-in."""
    response.headers["Cache-Control"] = "no-store"  # personal data
    return await service.get_me(session, caller)


@router.get(
    "",
    operation_id="staff_list",
    summary="Staff members, newest first (admins only)",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_staff(
    response: Response,
    _: CurrentStaff = Depends(require_admin),  # noqa: B008
    params: StaffListParams = Depends(staff_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaffPage:
    """Every staff member, deactivated ones included unless filtered out."""
    response.headers["Cache-Control"] = "no-store"  # personal data
    return await service.list_staff(session, params)


@router.post(
    "",
    operation_id="staff_invite",
    summary="Invite a staff member (admins only)",
    status_code=201,
    response_model=StaffRead,
    # Counted after the role check: only admins' invites count (20 an hour each).
    dependencies=[Depends(rate_limited(STAFF_INVITE, after=require_admin))],
    responses=problem_responses(
        ErrorCode.STAFF_EMAIL_TAKEN,
        ErrorCode.AUTH_ACCOUNT_EXISTS,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
    ),
)
async def invite(
    invitation: StaffInvite,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    auth: SupabaseAuthAdmin = Depends(auth_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Supabase emails them a link to set a password. They are active staff in `role` at
    once, and can sign in when they have accepted. Requires `Idempotency-Key`.

    - 409 STAFF_EMAIL_TAKEN: a staff member, active or not, has this email (reactivate them).
    - 409 AUTH_ACCOUNT_EXISTS: the email already has a sign-in account (an operator adds
      such people deliberately).
    - 503: Supabase could not send the invite; retrying with the same key is safe.
    """
    response = await service.invite(session, auth, caller, invitation, idempotent)
    response.headers["Cache-Control"] = "no-store"  # personal data
    return response


@router.get(
    "/{staff_id}",
    operation_id="staff_get",
    summary="One staff member (admins only)",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get_member(
    staff_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaffRead:
    response.headers["Cache-Control"] = "no-store"  # personal data
    return await service.get_member(session, staff_id)


@router.patch(
    "/{staff_id}",
    operation_id="staff_update",
    summary="Change a staff member's role or status (admins only)",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.LAST_ACTIVE_ADMIN),
)
async def update(
    staff_id: uuid.UUID,
    changes: StaffUpdate,
    response: Response,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaffRead:
    """Promote, demote, deactivate or reactivate. No Idempotency-Key: setting a value that
    is already there changes nothing.

    - 409 LAST_ACTIVE_ADMIN: the change would leave no active admin.
    """
    response.headers["Cache-Control"] = "no-store"  # personal data
    return await service.update(session, caller, staff_id, changes)
