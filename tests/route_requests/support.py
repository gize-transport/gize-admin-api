"""Route request test helpers."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import RouteRequestFactory

REQUESTS = "/v1/route-requests"


async def add_request(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    async with session.begin():
        return (await RouteRequestFactory().create(session, **values)).id


def request_path(request_id: uuid.UUID) -> str:
    return f"{REQUESTS}/{request_id}"
