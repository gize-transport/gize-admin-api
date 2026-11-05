"""GZ-040: the guard behind offers, called directly."""

import uuid

import httpx
import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.offers import service
from gize_api.offers.models import SeatOffer
from gize_api.riders import repository as riders_repository
from tests.factories import RiderFactory, RouteRequestFactory
from tests.offers.conftest import TODAY
from tests.offers.test_offers import PHONE, _offered, _request, _running
from tests.registrations.support import add_route, add_trip, verified
from tests.staff.support import add_staff, with_key


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


async def test_a_rider_made_meanwhile_with_the_phone_is_rider_exists(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asker signed up on the website between the check and the insert: the phone's
    index answers, the same 409."""
    trip = await add_trip(db_session)
    member = (await add_staff(db_session)).id
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = await _offered(api, member, route)
    async with db_session.begin():
        await RiderFactory().create(
            db_session, phone=PHONE, full_name="Someone Else",
            home_area_id=trip.bole, destination_area_id=trip.mexico,
        )  # fmt: skip

    async def never_taken(*args: object, **kwargs: object) -> bool:
        return False  # the check ran before the other insert committed

    monkeypatch.setattr(riders_repository, "phone_taken", never_taken)
    response = await api.post(f"/v1/offers/{offer}/accept", json={}, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (409, "RIDER_EXISTS")


async def test_an_acceptance_finished_between_peek_and_claim_replays(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tries with one key: the second missed the first's answer at its peek, then
    finds it at its claim and replays it."""
    trip = await add_trip(db_session)
    member = (await add_staff(db_session)).id
    route = await _running(db_session, trip)
    await _request(db_session, route, day=1)
    offer = await _offered(api, member, route)
    body = {"verified_phone_token": await verified(db_session, PHONE)}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await api.post(f"/v1/public/offers/{offer}/accept", json=body, headers=key)

    async def missed(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service, "peek", missed)
    again = await api.post(f"/v1/public/offers/{offer}/accept", json=body, headers=key)
    assert (again.status_code, again.json()) == (first.status_code, first.json())
