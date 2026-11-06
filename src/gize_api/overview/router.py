"""/v1/overview: the admin home page's summary (GZ-064)."""

from typing import Annotated

from fastapi import Depends, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.security import CurrentStaff, current_staff, staff_router
from gize_api.overview import service
from gize_api.overview.schemas import Overview

router = staff_router(prefix="/v1/overview", tags=["overview"])


class _NoParams(BaseModel):
    """Nothing to filter: an unknown parameter is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def no_params(params: Annotated[_NoParams, Query()]) -> None:
    return None


@router.get("", operation_id="overview_get", summary="Counts for the home page")
async def get(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    __: None = Depends(no_params),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Overview:
    """Live records only: routes by status (forming ones ready to launch, active ones with
    no driver), riders (riding today, reserved), open route requests and seat offers,
    active drivers. A snapshot: counts can move a moment later."""
    response.headers["Cache-Control"] = "no-store"
    return await service.overview(session)
