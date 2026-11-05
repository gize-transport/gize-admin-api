"""GZ-050: the guard behind driver changes, called directly."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.drivers import service
from gize_api.drivers.models import Driver


async def test_only_the_phone_and_plate_indexes_are_reported_as_taken(
    db_session: AsyncSession,
) -> None:
    """A phone or plate taken between the check and the insert: the index's violation
    is the check's 409; anything else stays an error (a bug, answered 500)."""
    async with db_session.begin():
        db_session.add(Driver(full_name="A", phone="+251911000001", address="Bole",
                              plate="AA 1-11111"))  # fmt: skip

    async def add(**values: str) -> None:
        async with db_session.begin():
            values = {
                "full_name": "B", "phone": f"+2519{uuid.uuid4().int % 10**8:08d}",
                "address": "CMC", "plate": f"AA 2-{uuid.uuid4().int % 10**5:05d}", **values,
            }  # fmt: skip
            db_session.add(Driver(**values))
            await service._flush(db_session)

    for taken, code in (
        ({"phone": "+251911000001"}, ErrorCode.DRIVER_EXISTS),
        ({"plate": "AA 1-11111"}, ErrorCode.PLATE_TAKEN),
    ):
        with pytest.raises(AppError) as raised:
            await add(**taken)
        assert raised.value.code is code
    with pytest.raises(IntegrityError) as other:
        await add(address=" untrimmed")  # the table refuses it
    assert violated_unique_constraint(other.value) is None
