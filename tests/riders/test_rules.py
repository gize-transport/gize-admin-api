"""GZ-034: the guards behind rider changes, called directly."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.riders import service
from tests.factories import RiderFactory

PHONE = "+251911234567"


async def test_only_the_phone_index_is_reported_as_a_rider_existing(
    db_session: AsyncSession,
) -> None:
    """The phone index's violation becomes RIDER_EXISTS; anything else stays an error (a
    bug, answered 500)."""
    async with db_session.begin():
        await RiderFactory().create(db_session, phone=PHONE)

    async def change_a_rider(**values: str) -> None:
        async with db_session.begin():
            rider = await RiderFactory().create(db_session)
            for field, value in values.items():
                setattr(rider, field, value)  # skipping the service's own checks
            await service._flush(db_session)

    with pytest.raises(AppError) as taken:
        await change_a_rider(phone=PHONE)
    assert taken.value.code is ErrorCode.RIDER_EXISTS
    with pytest.raises(IntegrityError) as raised:
        await change_a_rider(full_name=" untrimmed")  # the table refuses it
    assert violated_unique_constraint(raised.value) is None  # a check, not the phone index
