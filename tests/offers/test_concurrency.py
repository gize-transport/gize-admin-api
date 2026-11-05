"""GZ-040's acceptance: two acceptances of one offer at once, exactly one succeeds
(committed rows; the offer's row lock held until both wait on it)."""

import asyncio
import uuid
from datetime import UTC, date, datetime

import asyncpg
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.routes.models import AssignmentStatus, RouteAssignment, RouteStatus
from tests.factories import RouteRequestFactory
from tests.registrations.support import add_route, add_trip, verified
from tests.staff.support import add_staff, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

PHONE = "+251911000001"
LOCK_ROUTE = "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE"


async def test_two_acceptances_of_one_offer_one_succeeds(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Staff accept it for the caller while the caller accepts on the website."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        trip = await add_trip(session)
        route = await add_route(session, trip, status=RouteStatus.ACTIVE, seats=1, launch_minimum=1)
        async with session.begin():
            await RouteRequestFactory().create(
                session, kind="route", route_id=route, from_area_name=None, to_area_name=None,
                contact_phone=PHONE, created_at=datetime(2026, 10, 1, tzinfo=UTC),
            )  # fmt: skip
        token = await verified(session, PHONE)
    offered = await committed_api.post(
        f"/v1/routes/{route}/offers", json={}, headers=with_key(member)
    )
    assert offered.status_code == 201, offered.json()
    offer = offered.json()["id"]
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute(LOCK_ROUTE, route)
        tasks = [
            asyncio.create_task(
                committed_api.post(f"/v1/offers/{offer}/accept", json={},
                                   headers=with_key(member))
            ),
            asyncio.create_task(
                committed_api.post(f"/v1/public/offers/{offer}/accept",
                                   json={"verified_phone_token": token},
                                   headers={"Idempotency-Key": str(uuid.uuid4())})
            ),
        ]  # fmt: skip
        await wait_for_lock_waiters(database, count=2)
        await held.rollback()
        responses = await asyncio.gather(*tasks)
    finally:
        await conn.close()
    assert sorted(response.status_code for response in responses) == [200, 409]
    (refused,) = [response for response in responses if response.status_code == 409]
    assert refused.json()["code"] == "OFFER_CLOSED"
    async with committed_sessionmaker() as session, session.begin():
        seats = (await session.scalars(select(RouteAssignment))).all()
    assert [(seat.status, seat.start_date) for seat in seats] == [
        (AssignmentStatus.ACTIVE, date(2026, 12, 1))
    ]
