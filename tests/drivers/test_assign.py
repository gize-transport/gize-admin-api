"""GZ-053: a driver for each route (owner decision, 2026-11-05: active drivers only, no
approval or vehicle seats in the MVP).

`PUT /v1/routes/{id}/driver {driver_id}` gives the route its driver, replaces them, or
clears it (`null`). A driver may cover several routes whose runs don't overlap in time
(the morning run leaves A to arrives B, the return run leaves B to arrives A): otherwise
409 DRIVER_BUSY, also when a route's times change. A driver who drives a route can't be
deactivated or deleted (409 DRIVER_ASSIGNED).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.db import get_session
from gize_api.drivers.models import Driver
from gize_api.main import GizeAPI
from tests.routes.support import add_route
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in
from tests.support import DatabaseUrls, wait_for_lock_waiters


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def _driver(session: AsyncSession, **values: object) -> uuid.UUID:
    async with session.begin():
        driver = Driver(
            **{
                "full_name": "Tesfaye Alemu",
                "phone": f"+2519{uuid.uuid4().int % 10**8:08d}",
                "address": "Bole",
                "plate": f"AA 3-{uuid.uuid4().int % 10**5:05d}",
                **values,
            }
        )
        session.add(driver)
        await session.flush()
        return driver.id


async def _route(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    """06:30 → 07:15 in the morning, 17:30 → 18:15 back, unless told otherwise."""
    return await add_route(session, **values)


async def _assign(
    api: httpx.AsyncClient, staff: uuid.UUID, route: uuid.UUID, driver: uuid.UUID | None
) -> httpx.Response:
    return await api.put(
        f"/v1/routes/{route}/driver",
        json={"driver_id": None if driver is None else str(driver)},
        headers=signed_in(staff),
    )


async def test_staff_give_a_route_its_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session, plate="AA 3-B12345")
    route = await _route(db_session)
    response = await _assign(api, member, route, driver)
    assert response.status_code == 200, response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["driver"] == {
        "id": str(driver), "full_name": "Tesfaye Alemu", "plate": "AA 3-B12345",
    }  # fmt: skip
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.before, entry.after) == (
        "route.driver_changed", {"driver_id": None}, {"driver_id": str(driver)},
    )  # fmt: skip
    listed = await api.get("/v1/routes", headers=signed_in(member))
    assert [item["driver"]["id"] for item in listed.json()["items"]] == [str(driver)]


async def test_a_driver_is_replaced_or_cleared(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    first, second = await _driver(db_session), await _driver(db_session)
    route = await _route(db_session)
    await _assign(api, member, route, first)
    replaced = await _assign(api, member, route, second)
    assert replaced.json()["driver"]["id"] == str(second)
    cleared = await _assign(api, member, route, None)
    assert cleared.json()["driver"] is None
    again = await _assign(api, member, route, None)  # nothing to change, nothing recorded
    assert again.status_code == 200
    assert len(await audit_entries(db_session, route)) == 3  # given, replaced, cleared


@pytest.mark.parametrize("problem", ["unknown", "deleted", "inactive"])
async def test_only_an_active_driver_drives(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID, problem: str
) -> None:
    driver = uuid.uuid4()
    if problem == "deleted":
        driver = await _driver(db_session, deleted_at=datetime.now(UTC))
    if problem == "inactive":
        driver = await _driver(db_session, is_active=False)
    response = await _assign(api, member, await _route(db_session), driver)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert [error["field"] for error in response.json()["errors"]] == ["/driver_id"]


@pytest.mark.parametrize(
    ("times", "busy"),
    [
        ({}, True),  # the same runs
        ({"morning_time": time(7, 0), "morning_arrival_time": time(7, 45)}, True),
        ({"morning_time": time(7, 15), "morning_arrival_time": time(8, 0),
          "return_time": time(16, 0), "return_arrival_time": time(16, 45)}, False),  # touching
        ({"morning_time": time(8, 0), "morning_arrival_time": time(8, 45),
          "return_time": time(18, 0), "return_arrival_time": time(19, 0)}, True),  # evening
        ({"morning_time": time(5, 0), "morning_arrival_time": time(5, 45),
          "return_time": time(19, 0), "return_arrival_time": time(19, 45)}, False),
        # Made before arrival times existed: a departure alone.
        ({"morning_arrival_time": None, "return_arrival_time": None}, True),
        ({"morning_time": time(7, 0), "morning_arrival_time": None,
          "return_time": time(18, 0), "return_arrival_time": None}, True),  # under way then
        ({"morning_time": time(7, 15), "morning_arrival_time": None,
          "return_time": time(18, 15), "return_arrival_time": None}, False),  # just after
    ],
    ids=["same", "morning overlaps", "touching", "evening overlaps", "apart",
         "no arrivals, same departures", "no arrivals, during a run", "no arrivals, after"],
)  # fmt: skip
async def test_a_driver_covers_routes_whose_runs_dont_overlap(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    member: uuid.UUID,
    times: dict[str, time],
    busy: bool,
) -> None:
    driver = await _driver(db_session)
    first = await _route(db_session)
    assert (await _assign(api, member, first, driver)).status_code == 200
    second = await _route(db_session, **times)
    response = await _assign(api, member, second, driver)
    if busy:
        assert (response.status_code, response.json()["code"]) == (409, "DRIVER_BUSY")
    else:
        assert response.status_code == 200, response.json()


async def test_a_deleted_route_frees_its_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    await _route(db_session, driver_id=driver, deleted_at=datetime.now(UTC))
    response = await _assign(api, member, await _route(db_session), driver)
    assert response.status_code == 200


async def test_new_times_must_still_suit_the_routes_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    first = await _route(db_session)
    second = await _route(
        db_session, morning_time=time(8, 0), morning_arrival_time=time(8, 45),
        return_time=time(16, 0), return_arrival_time=time(16, 45),
    )  # fmt: skip
    await _assign(api, member, first, driver)
    await _assign(api, member, second, driver)
    response = await api.patch(
        f"/v1/routes/{second}",
        json={"morning_time": "06:00", "morning_arrival_time": "07:00"},
        headers=signed_in(member),
    )
    assert (response.status_code, response.json()["code"]) == (409, "DRIVER_BUSY")
    fine = await api.patch(
        f"/v1/routes/{second}", json={"code": "RENAMED-01"}, headers=signed_in(member)
    )
    assert fine.status_code == 200


async def test_a_driver_who_drives_stays_active_and_live(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    admin = (await add_admin(db_session)).id
    driver = await _driver(db_session)
    route = await _route(db_session)
    await _assign(api, member, route, driver)
    deactivated = await api.patch(
        f"/v1/drivers/{driver}", json={"is_active": False}, headers=signed_in(member)
    )
    assert (deactivated.status_code, deactivated.json()["code"]) == (409, "DRIVER_ASSIGNED")
    deleted = await api.delete(f"/v1/drivers/{driver}", headers=signed_in(admin))
    assert (deleted.status_code, deleted.json()["code"]) == (409, "DRIVER_ASSIGNED")
    edited = await api.patch(
        f"/v1/drivers/{driver}", json={"address": "CMC"}, headers=signed_in(member)
    )
    assert edited.status_code == 200  # other changes are fine
    await _assign(api, member, route, None)
    assert (await api.delete(f"/v1/drivers/{driver}", headers=signed_in(admin))).status_code == 204


@pytest.mark.parametrize("body", [{}, {"driver_id": "d1"}, {"driver_id": None, "x": 1}], ids=str)
async def test_a_bad_body_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID, body: object
) -> None:
    route = await _route(db_session)
    response = await api.put(f"/v1/routes/{route}/driver", json=body, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_an_unknown_route_is_404(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    response = await _assign(api, member, uuid.uuid4(), await _driver(db_session))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    member: uuid.UUID,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await _assign(api, member, uuid.uuid4(), uuid.uuid4())
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


async def test_one_driver_given_two_overlapping_routes_at_once_gets_one(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Each assignment locks its route, then the driver: the second waits, then sees the
    first route taken (DRIVER_BUSY)."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        driver = await _driver(session)
        routes = [await _route(session), await _route(session)]
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute("SELECT 1 FROM gize.drivers WHERE id = $1 FOR UPDATE", driver)
        tasks = [
            asyncio.create_task(_assign(committed_api, member, route, driver)) for route in routes
        ]
        await wait_for_lock_waiters(database, count=2)
        await held.rollback()
        responses = await asyncio.gather(*tasks)
    finally:
        await conn.close()
    assert sorted(response.status_code for response in responses) == [200, 409]
    (refused,) = [response for response in responses if response.status_code == 409]
    assert refused.json()["code"] == "DRIVER_BUSY"


async def test_routes_without_arrival_times_clash_on_the_same_departures(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    """Made before arrival times existed: a departure alone. The same departure is the
    same moment; a newer route under way then clashes with it too."""
    driver = await _driver(db_session)
    older = {"morning_arrival_time": None, "return_arrival_time": None}

    async def assigned(**times: Any) -> int:  # noqa: ANN401
        response = await _assign(api, member, await _route(db_session, **times), driver)
        return int(response.status_code)

    assert await assigned(**older) == 200
    assert await assigned(**older) == 409
    assert await assigned(morning_time=time(6, 0), morning_arrival_time=time(7, 0)) == 409
    assert await assigned(**{**older, "morning_time": time(8, 0),
                             "return_time": time(16, 0)}) == 200  # fmt: skip


async def test_giving_a_route_the_driver_it_has_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    route = await _route(db_session)
    await _assign(api, member, route, driver)
    again = await _assign(api, member, route, driver)
    assert again.status_code == 200
    assert len(await audit_entries(db_session, route)) == 1


async def test_a_driver_whose_route_was_deleted_may_be_deactivated(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    await _route(db_session, driver_id=driver, deleted_at=datetime.now(UTC))
    response = await api.patch(
        f"/v1/drivers/{driver}", json={"is_active": False}, headers=signed_in(member)
    )
    assert (response.status_code, response.json()["is_active"]) == (200, False)
