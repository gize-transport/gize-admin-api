"""/v1/staff: who is signed in (GZ-011); staff management follows in GZ-013."""

from fastapi import Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.security import CurrentStaff, current_staff, staff_router
from gize_api.staff import service
from gize_api.staff.schemas import StaffMe

router = staff_router(prefix="/v1/staff", tags=["staff"])


@router.get(
    "/me",
    operation_id="staff_me",
    summary="The signed-in staff member",
    responses={401: {}, 403: {}, 429: {}, 503: {}},
)
async def me(
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StaffMe:
    """Your ID, name, email and role. The admin portal calls it after sign-in."""
    response.headers["Cache-Control"] = "no-store"  # personal data
    return await service.get_me(session, caller)
