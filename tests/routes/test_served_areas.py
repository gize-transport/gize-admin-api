"""GZ-023: the areas a route serves between A and B (owner decision: a route is A to B; no
stops with their own times). Riders board or get off at A, B or one of these areas.

Staff set the whole list at once, in order. It's shown on every route.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from gize_api.core.db import get_session
from gize_api.corridors.models import Corridor
from gize_api.main import GizeAPI, create_app
from gize_api.routes.models import Route, RouteArea
from tests.factories import AreaFactory, RouteFactory
from tests.routes.support import ROUTES, add_corridor, add_route, route_path
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in
from tests.support import DatabaseUrls, SettingsFactory, wait_for_lock_waiters

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


def _areas_path(route_id: uuid.UUID) -> str:
    return f"{route_path(route_id)}/areas"


async def _new_areas(session: AsyncSession, *names: str) -> list[uuid.UUID]:
    async with session.begin():
        return [(await AreaFactory().create(session, name=name)).id for name in names]


async def _ends(session: AsyncSession, route_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    async with session.begin():
        route = await session.get_one(Route, route_id)
        corridor = await session.get_one(Corridor, route.corridor_id)
        return corridor.origin_area_id, corridor.destination_area_id


def _ids(*area_ids: uuid.UUID) -> dict[str, list[str]]:
    return {"area_ids": [str(area_id) for area_id in area_ids]}


async def test_staff_set_the_areas_a_route_serves(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    cmc, summit = await _new_areas(db_session, "CMC", "Summit")
    response = await api.put(
        _areas_path(route), json=_ids(cmc, summit), headers=signed_in(member.id)
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["served_areas"] == [
        {"id": str(cmc), "name": "CMC"},
        {"id": str(summit), "name": "Summit"},
    ]
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.actor_staff_id) == ("route.areas_changed", member.id)
    assert (entry.before, entry.after) == (
        {"served_area_ids": []},
        {"served_area_ids": [str(cmc), str(summit)]},
    )
    shown = (await api.get(route_path(route), headers=signed_in(member.id))).json()
    assert [area["name"] for area in shown["served_areas"]] == ["CMC", "Summit"]


async def test_the_list_is_replaced_whole(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    cmc, summit, gerji = await _new_areas(db_session, "CMC", "Summit", "Gerji")
    await api.put(_areas_path(route), json=_ids(cmc, summit), headers=signed_in(member.id))
    response = await api.put(
        _areas_path(route), json=_ids(gerji, cmc), headers=signed_in(member.id)
    )
    assert [area["name"] for area in response.json()["served_areas"]] == ["Gerji", "CMC"]
    cleared = await api.put(_areas_path(route), json={"area_ids": []}, headers=signed_in(member.id))
    assert cleared.json()["served_areas"] == []


async def test_the_same_list_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    (cmc,) = await _new_areas(db_session, "CMC")
    for _ in range(2):
        await api.put(_areas_path(route), json=_ids(cmc), headers=signed_in(member.id))
    assert len(await audit_entries(db_session, route)) == 1


@pytest.mark.parametrize(
    "problem", ["repeated", "origin", "destination", "unknown", "deleted", "too_many"]
)
async def test_invalid_lists_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, problem: str
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    origin, destination = await _ends(db_session, route)
    (cmc,) = await _new_areas(db_session, "CMC")
    async with db_session.begin():
        gone = (await AreaFactory().create(db_session, deleted_at=START)).id
        many = [(await AreaFactory().create(db_session)).id for _ in range(21)]
    area_ids = {
        "repeated": [cmc, cmc],
        "origin": [origin, cmc],  # A and B are the corridor's, not served areas
        "destination": [cmc, destination],
        "unknown": [uuid.uuid4()],
        "deleted": [gone],
        "too_many": many,
    }[problem]
    response = await api.put(_areas_path(route), json=_ids(*area_ids), headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [e["field"] for e in response.json()["errors"]] == ["/area_ids"]


@pytest.mark.parametrize(
    "body", [{}, {"area_ids": None}, {"area_ids": ["nope"]}, {"area_ids": [], "x": 1}]
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    response = await api.put(_areas_path(route), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("deleted", [False, True])
async def test_an_unknown_or_deleted_route_is_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, deleted_at=START) if deleted else uuid.uuid4()
    response = await api.put(
        _areas_path(route), json={"area_ids": []}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_a_route_serving_areas_keeps_its_corridor_until_they_are_cleared(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The served areas lie between this corridor's A and B (in its direction): another
    corridor, even the way back, would make them wrong."""
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session)
    (cmc,) = await _new_areas(db_session, "CMC")
    await api.put(_areas_path(route), json=_ids(cmc), headers=signed_in(member_id))
    other = await add_corridor(db_session)
    move = {"corridor_id": str(other)}
    refused = await api.patch(route_path(route), json=move, headers=signed_in(member_id))
    assert [e["field"] for e in refused.json()["errors"]] == ["/corridor_id"]
    await api.put(_areas_path(route), json={"area_ids": []}, headers=signed_in(member_id))
    assert (
        await api.patch(route_path(route), json=move, headers=signed_in(member_id))
    ).status_code == 200


async def test_an_area_a_live_route_serves_cannot_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    route, gone_route = await add_route(db_session), await add_route(db_session)
    cmc, gerji = await _new_areas(db_session, "CMC", "Gerji")
    await api.put(_areas_path(route), json=_ids(cmc), headers=signed_in(admin_id))
    await api.put(_areas_path(gone_route), json=_ids(gerji), headers=signed_in(admin_id))
    async with db_session.begin():
        (await db_session.get_one(Route, gone_route)).deleted_at = START
    used = await api.delete(f"/v1/areas/{cmc}", headers=signed_in(admin_id))
    assert (used.status_code, used.json()["code"]) == (409, "AREA_IN_USE")
    assert (await api.delete(f"/v1/areas/{gerji}", headers=signed_in(admin_id))).status_code == 204


async def test_a_page_of_routes_takes_the_same_queries_however_many_areas(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        for _ in range(rows):
            route = await add_route(db_session)
            area_ids = await _new_areas(
                db_session, f"A{uuid.uuid4().hex[:6]}", f"B{uuid.uuid4().hex[:6]}"
            )
            await api.put(_areas_path(route), json=_ids(*area_ids), headers=signed_in(member.id))
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(ROUTES, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts[0] == counts[1] <= 5


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    path = _areas_path(uuid.UUID(int=1))
    assert (await api.put(path, json={"area_ids": []})).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        assert (
            await api.put(path, json={"area_ids": []}, headers=signed_in(caller))
        ).status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.put(
        _areas_path(uuid.UUID(int=1)), json={"area_ids": []}, headers=signed_in(uuid.uuid4())
    )
    assert response.status_code == 503


async def test_a_list_waiting_on_an_area_delete_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session)
            area = await AreaFactory().create(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.areas WHERE id = $1 FOR UPDATE", area.id)
            request = asyncio.create_task(
                committed_api.put(
                    _areas_path(route.id), json=_ids(area.id), headers=signed_in(member.id)
                )
            )
            await wait_for_lock_waiters(database)
            await conn.execute("UPDATE gize.areas SET deleted_at = now() WHERE id = $1", area.id)
    finally:
        await conn.close()
    response = await request
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/area_ids"]
    async with committed_sessionmaker() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(RouteArea)) == 0


async def test_an_area_delete_waiting_on_a_new_list_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            route = await RouteFactory().create(session)
            area = await AreaFactory().create(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.areas WHERE id = $1 FOR SHARE", area.id)
            request = asyncio.create_task(
                committed_api.delete(f"/v1/areas/{area.id}", headers=signed_in(admin.id))
            )
            await wait_for_lock_waiters(database)
            await conn.execute(
                "INSERT INTO gize.route_areas (route_id, area_id, position) VALUES ($1, $2, 1)",
                route.id,
                area.id,
            )
    finally:
        await conn.close()
    response = await request
    assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")


async def test_two_lists_at_once_leave_one_whole(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Both take the route's row lock: one list, then the other, never a mix."""
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session)
            first = [(await AreaFactory().create(session)).id for _ in range(3)]
            second = [(await AreaFactory().create(session)).id for _ in range(3)]
    responses = await asyncio.gather(
        *(
            committed_api.put(_areas_path(route.id), json=_ids(*ids), headers=signed_in(member.id))
            for ids in (first, second)
        )
    )
    assert [r.status_code for r in responses] == [200, 200]
    async with committed_sessionmaker() as session, session.begin():
        rows = await session.execute(
            select(RouteArea.area_id, RouteArea.position)
            .where(RouteArea.route_id == route.id)
            .order_by(RouteArea.position)
        )
        stored = [(area_id, position) for area_id, position in rows]
    assert [position for _, position in stored] == [1, 2, 3]
    assert [area_id for area_id, _ in stored] in (first, second)


async def test_setting_areas_marks_the_route_updated(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            route = await RouteFactory().create(session)
            area = await AreaFactory().create(session)
    before = (await committed_api.get(route_path(route.id), headers=signed_in(member.id))).json()
    after = await committed_api.put(
        _areas_path(route.id), json=_ids(area.id), headers=signed_in(member.id)
    )
    assert after.json()["updated_at"] > before["updated_at"]


def test_areas_served_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}/areas"]["put"]
    assert operation["operationId"] == "routes_set_areas"
    assert operation["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]
    body = schema["components"]["schemas"]["RouteAreasSet"]
    assert body["properties"]["area_ids"]["maxItems"] == 20
    served = schema["components"]["schemas"]["RouteRead"]["properties"]["served_areas"]
    assert served["items"] == {"$ref": "#/components/schemas/AreaRef"}
