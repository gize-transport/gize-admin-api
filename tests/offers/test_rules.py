"""GZ-040: the guard behind offers, called directly."""

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.offers import service
from gize_api.offers.models import SeatOffer
from tests.factories import RouteRequestFactory
from tests.offers.conftest import TODAY
from tests.registrations.support import add_route, add_trip


async def test_only_the_open_offer_index_is_reported_as_an_offer_open(
    db_session: AsyncSession,
) -> None:
    """Two offers to one request at once: the index's violation is the same 409 the
    check gives; anything else stays an error."""
    route = await add_route(db_session, await add_trip(db_session))
    async with db_session.begin():
        request = (await RouteRequestFactory().create(db_session)).id

    async def add(**values: object) -> None:
        async with db_session.begin():
            db_session.add(
                SeatOffer(route_id=route, request_id=request, start_date=TODAY,
                          expires_at=func.now(), **values)
            )  # fmt: skip
            await service._flush(db_session)

    await add()
    with pytest.raises(AppError) as taken:
        await add()
    assert taken.value.code is ErrorCode.OFFER_OPEN
    with pytest.raises(IntegrityError) as raised:
        await add(status="accepted")  # accepted needs its rider: a check, not the index
    assert violated_unique_constraint(raised.value) is None
