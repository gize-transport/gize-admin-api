"""GZ-032: sign-ups at the same time (committed rows, each request its own connection).

A seat is counted and taken under the route's lock, so the last seat goes to one rider;
a phone signs up once; a token is spent once; a sign-up waiting on the route's lock reads
the route as it is after the wait.
"""

import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any

import asyncpg
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.riders.models import Rider
from gize_api.routes.models import RouteAssignment
from gize_api.verification.tokens import read_verified_phone_token
from tests.registrations.support import ADULT, add_route, add_trip, body, key, verified
from tests.support import DatabaseUrls, wait_for_lock_waiters
from tests.verification.support import TOKEN_KEY

OTHER_PHONE = "+251922334455"


async def _count[M](sessions: async_sessionmaker[AsyncSession], model: type[M]) -> int:
    async with sessions() as session, session.begin():
        return len((await session.scalars(select(model))).all())


# Fixed statements: no SQL is built from strings, even in tests.
LOCKS = {
    "routes": "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE",
    "areas": "SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE",
    "phone_verifications": "SELECT 1 FROM gize.phone_verifications WHERE id = $1 FOR UPDATE",
}


async def _released_together(
    database: DatabaseUrls,
    table: str,
    row_id: uuid.UUID,
    sign_ups: list[Coroutine[Any, Any, httpx.Response]],
) -> list[httpx.Response]:
    """Hold a row lock until every sign-up waits on it, then let them go at once: they
    really overlap, whatever the scheduler does."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute(LOCKS[table], row_id)
        tasks = [asyncio.create_task(sign_up) for sign_up in sign_ups]
        await wait_for_lock_waiters(database, count=len(tasks))
        await held.rollback()
        return list(await asyncio.gather(*tasks))
    finally:
        await conn.close()


async def test_two_riders_for_the_last_seat_one_gets_it(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        trip = await add_trip(session)
        route = await add_route(session, trip, seats=1, launch_minimum=1)
        tokens = [await verified(session), await verified(session, OTHER_PHONE)]
    responses = await _released_together(
        database,
        "routes",
        route,
        [
            committed_api.post(ADULT, json=body(token, trip, route_id=str(route)), headers=key())
            for token in tokens
        ],
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    (refused,) = [response for response in responses if response.status_code == 409]
    assert refused.json()["code"] == "ROUTE_FULL"
    assert await _count(committed_sessionmaker, RouteAssignment) == 1
    assert await _count(committed_sessionmaker, Rider) == 1


async def test_one_phone_signs_up_once(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Both pass the "no rider yet" check together: the phone's unique index decides, and
    the loser's violation is the same 409."""
    async with committed_sessionmaker() as session:
        trip = await add_trip(session)
        tokens = [await verified(session), await verified(session)]  # two codes confirmed
    responses = await _released_together(
        database,
        "areas",
        max(trip.bole, trip.mexico),  # locked last: both have spent their tokens by then
        [committed_api.post(ADULT, json=body(token, trip), headers=key()) for token in tokens],
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    (refused,) = [response for response in responses if response.status_code == 409]
    assert refused.json()["code"] == "RIDER_EXISTS"
    assert await _count(committed_sessionmaker, Rider) == 1


async def test_one_token_is_spent_once(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Two keys, one token: the verification's row lock lets one through."""
    async with committed_sessionmaker() as session:
        trip = await add_trip(session)
        token = await verified(session)
    verification = read_verified_phone_token(token, key=TOKEN_KEY).verification_id
    responses = await _released_together(
        database,
        "phone_verifications",
        verification,
        [committed_api.post(ADULT, json=body(token, trip), headers=key()) for _ in "ab"],
    )
    assert sorted(response.status_code for response in responses) == [201, 401]
    assert await _count(committed_sessionmaker, Rider) == 1


async def test_a_retry_racing_the_first_try_replays_it(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The first try holds the key (not answered yet) when its retry arrives: the retry
    waits for it, then replays its answer instead of signing up twice."""
    async with committed_sessionmaker() as session:
        trip = await add_trip(session)
        token = await verified(session)
    request, headers = body(token, trip), key()
    verification = read_verified_phone_token(token, key=TOKEN_KEY).verification_id
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute(LOCKS["phone_verifications"], verification)
        first = asyncio.create_task(committed_api.post(ADULT, json=request, headers=headers))
        await wait_for_lock_waiters(database)  # it has claimed the key, then waits here
        retry = asyncio.create_task(committed_api.post(ADULT, json=request, headers=headers))
        await wait_for_lock_waiters(database, count=2)  # it waits on the claimed key
        await held.rollback()
        responses = [await first, await retry]
    finally:
        await conn.close()
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[1].json() == responses[0].json()
    assert await _count(committed_sessionmaker, Rider) == 1


async def test_a_sign_up_waiting_on_a_launch_asks_instead(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The route is launched while the sign-up waits for its lock: after the wait it is
    active, so the rider gets a request, not a reservation the launch never counted."""
    async with committed_sessionmaker() as session:
        trip = await add_trip(session)
        route = await add_route(session, trip)
        token = await verified(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        launch = conn.transaction()
        await launch.start()
        await conn.execute(LOCKS["routes"], route)
        await conn.execute("UPDATE gize.routes SET status = 'active' WHERE id = $1", route)
        sign_up = asyncio.create_task(
            committed_api.post(ADULT, json=body(token, trip, route_id=str(route)), headers=key())
        )
        await wait_for_lock_waiters(database)
        await launch.commit()
        response = await sign_up
    finally:
        await conn.close()
    assert (response.status_code, response.json()["outcome"]) == (201, "requested")
    assert await _count(committed_sessionmaker, RouteAssignment) == 0
