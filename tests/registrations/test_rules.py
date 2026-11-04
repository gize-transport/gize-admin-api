"""GZ-032: the guards behind the sign-up, called directly."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import violated_unique_constraint
from gize_api.registrations import service
from gize_api.registrations.schemas import AdultRegistration
from gize_api.riders.models import RiderPlan
from gize_api.routes import seats
from gize_api.routes.models import Route, RouteStatus
from tests.registrations.conftest import TODAY
from tests.registrations.support import PHONE, add_route, add_trip


async def test_only_forming_routes_take_reservations(db_session: AsyncSession) -> None:
    """Callers decide; a running route reaching here is a bug, not a seat."""
    trip = await add_trip(db_session)
    route_id = await add_route(db_session, trip, status=RouteStatus.ACTIVE)
    async with db_session.begin():
        route = await db_session.get_one(Route, route_id)
        with pytest.raises(ValueError, match="forming"):
            await seats.reserve(db_session, route, uuid.uuid4(), TODAY)


async def test_other_constraint_errors_are_not_reported_as_a_rider_existing(
    db_session: AsyncSession,
) -> None:
    """Only the phone index's violation becomes RIDER_EXISTS; anything else stays an error
    (a bug, answered 500)."""
    trip = await add_trip(db_session)
    untrimmed = AdultRegistration.model_construct(
        verified_phone_token="",
        full_name=" Abebe",  # skipped validation: the table refuses it
        home_area_id=trip.bole,
        destination_area_id=trip.mexico,
        plan=RiderPlan.ROUND_TRIP,
        saturday=False,
    )
    with pytest.raises(IntegrityError) as raised:
        async with db_session.begin():
            await service._new_rider(db_session, untrimmed, PHONE)
    assert violated_unique_constraint(raised.value) is None  # a check, not the phone index
