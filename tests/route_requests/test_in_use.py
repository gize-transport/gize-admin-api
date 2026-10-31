"""GZ-027: what open requests use stays: an area or route an open request names can't be
deleted. Requests share-lock what they name while they're recorded or matched, and the
deletes take an update lock, so the two never cross (committed rows for the races)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.factories import AreaFactory, RouteFactory
from tests.route_requests.support import REQUESTS, add_request, request_path
from tests.routes.support import add_route
from tests.staff.support import add_admin, add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
CALLER = {
    "rider_type": "adult",
    "rider_name": "Abebe Kebede",
    "contact_name": "Abebe Kebede",
    "contact_phone": "0911234567",
}

# Fixed statements: no SQL is built from strings, even in tests.
LOCKS = {
    ("areas", "UPDATE"): "SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE",
    ("areas", "SHARE"): "SELECT 1 FROM gize.areas WHERE id = $1 FOR SHARE",
    ("routes", "UPDATE"): "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE",
    ("routes", "SHARE"): "SELECT 1 FROM gize.routes WHERE id = $1 FOR SHARE",
}
_REQUEST_FOR_AREA = (
    "INSERT INTO gize.route_requests (kind, from_area_id, to_area_name, rider_type,"
    " rider_name, contact_name, contact_phone) VALUES ('new_areas', $1, 'Bole', 'adult',"
    " 'Abebe', 'Abebe', '+251911234567')"
)
_REQUEST_FOR_ROUTE = (
    "INSERT INTO gize.route_requests (kind, route_id, rider_type, rider_name, contact_name,"
    " contact_phone) VALUES ('route', $1, 'adult', 'Abebe', 'Abebe', '+251911234567')"
)


@contextlib.asynccontextmanager
async def _holding(
    database: DatabaseUrls, table: str, row_id: uuid.UUID, mode: str
) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute(LOCKS[table, mode], row_id)
            yield conn
    finally:
        await conn.close()


async def test_an_area_an_open_request_names_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    async with db_session.begin():
        named, to_side, closed = await AreaFactory().create_batch(db_session, 3)
        ids = (named.id, to_side.id, closed.id)
    await add_request(db_session, from_area_id=ids[0], from_area_name=None)
    await add_request(db_session, to_area_id=ids[1], to_area_name=None)
    await add_request(
        db_session, to_area_id=ids[2], to_area_name=None, status="declined",
        decline_reason="Too far.",
    )  # fmt: skip
    for area in ids[:2]:
        response = await api.delete(f"/v1/areas/{area}", headers=signed_in(admin_id))
        assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")
    assert (await api.delete(f"/v1/areas/{ids[2]}", headers=signed_in(admin_id))).status_code == 204


async def test_a_route_an_open_request_names_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    asked_id = await add_route(db_session, status="active")
    matched_id = await add_route(db_session, status="active")
    closed_id = await add_route(db_session, status="active")
    await add_request(
        db_session, kind="route", route_id=asked_id, from_area_name=None, to_area_name=None
    )
    await add_request(db_session, status="matched", matched_route_id=matched_id)
    await add_request(db_session, status="fulfilled", matched_route_id=closed_id)
    for route in (asked_id, matched_id):
        response = await api.delete(f"/v1/routes/{route}", headers=signed_in(admin_id))
        assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_REQUESTS")
    response = await api.delete(f"/v1/routes/{closed_id}", headers=signed_in(admin_id))
    assert response.status_code == 204


async def test_two_requests_at_once_record_one(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
    body = {"kind": "new_areas", "from_area_name": "Ayat", "to_area_name": "Bole", **CALLER}
    responses = await asyncio.gather(
        *(committed_api.post(REQUESTS, json=body, headers=with_key(member.id)) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    refused = next(r for r in responses if r.status_code == 409)
    assert refused.json()["code"] == "ROUTE_REQUEST_EXISTS"


async def test_a_request_waiting_on_an_area_delete_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            area = await AreaFactory().create(session)
    body = {"kind": "new_areas", "from_area_id": str(area.id), "to_area_name": "Bole", **CALLER}
    async with _holding(database, "areas", area.id, "UPDATE") as conn:
        request = asyncio.create_task(
            committed_api.post(REQUESTS, json=body, headers=with_key(member.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.areas SET deleted_at = now() WHERE id = $1", area.id)
    response = await request
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/from_area_id"]


async def test_an_area_delete_waiting_on_a_new_request_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            area = await AreaFactory().create(session)
    async with _holding(database, "areas", area.id, "SHARE") as conn:
        request = asyncio.create_task(
            committed_api.delete(f"/v1/areas/{area.id}", headers=signed_in(admin.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute(_REQUEST_FOR_AREA, area.id)
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")


async def test_a_match_waiting_on_a_route_delete_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session)
    request_id = await add_request_committed(committed_sessionmaker)
    body = {"status": "matched", "matched_route_id": str(route.id)}
    async with _holding(database, "routes", route.id, "UPDATE") as conn:
        request = asyncio.create_task(
            committed_api.put(
                f"{request_path(request_id)}/status", json=body, headers=signed_in(member.id)
            )
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.routes SET deleted_at = now() WHERE id = $1", route.id)
    response = await request
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/matched_route_id"]


async def test_a_route_delete_waiting_on_a_new_request_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            route = await RouteFactory().create(session, status="active")
    async with _holding(database, "routes", route.id, "SHARE") as conn:
        request = asyncio.create_task(
            committed_api.delete(f"/v1/routes/{route.id}", headers=signed_in(admin.id))
        )
        await wait_for_lock_waiters(database)
        await conn.execute(_REQUEST_FOR_ROUTE, route.id)
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_REQUESTS")


async def add_request_committed(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        return await add_request(session)
