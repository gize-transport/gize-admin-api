"""GZ-028: add a van, by copying a route.

The copy keeps the corridor, type, school or company, times, prices (staff may copy them,
never change them) and the areas served, in order; it gets its own code, seats and launch
minimum, and names the original (`copied_from_route_id`). An individual route's copy starts
`forming` when the original has both prices, else `draft`; a corporate copy starts `draft`.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.routes.models import Route, RouteArea
from tests.factories import AreaFactory, CompanyFactory, RouteFactory
from tests.routes.support import ROUTES, add_company, add_route, route_path
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import DatabaseUrls, SettingsFactory, wait_for_lock_waiters

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
PRICED = {"price_round_trip": 2400, "price_one_way": 1400, "price_saturday": 300}
VAN = {"code": "ayat-02", "seats": 12, "launch_minimum": 8}


def copies_path(route_id: uuid.UUID) -> str:
    return f"{route_path(route_id)}/copies"


async def _serve(session: AsyncSession, route_id: uuid.UUID, *names: str) -> list[uuid.UUID]:
    """Give the route these served areas, in order."""
    async with session.begin():
        areas = [await AreaFactory().create(session, name=name) for name in names]
        for position, area in enumerate(areas, start=1):
            session.add(RouteArea(route_id=route_id, area_id=area.id, position=position))
        await session.flush()
        return [area.id for area in areas]


async def test_staff_add_a_van_by_copying_a_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    original = await add_route(
        db_session, code="AYAT-01", status="active", seats=14, launch_minimum=10, **PRICED
    )
    served = await _serve(db_session, original, "CMC", "Summit")
    source = (await api.get(route_path(original), headers=signed_in(member.id))).json()
    response = await api.post(copies_path(original), json=VAN, headers=with_key(member.id))
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    copy = response.json()
    assert response.headers["Location"] == f"{ROUTES}/{copy['id']}"
    kept = (
        "corridor", "route_type", "school_name", "company", "morning_time",
        "morning_arrival_time", "return_time", "return_arrival_time", "served_areas", "prices",
    )  # fmt: skip
    assert {key: copy[key] for key in kept} == {key: source[key] for key in kept}
    assert [area["id"] for area in copy["served_areas"]] == [str(a) for a in served]
    assert (copy["code"], copy["seats"], copy["launch_minimum"]) == ("AYAT-02", 12, 8)
    assert (copy["status"], copy["copied_from_route_id"]) == ("forming", str(original))
    assert copy["seats_taken"] == {"this_month": 0, "next_month": 0}
    copy_id = uuid.UUID(copy["id"])
    entries = {e.action: e for e in await audit_entries(db_session, copy_id)}
    assert set(entries) == {"route.copied", "route.areas_changed"}
    copied = entries["route.copied"]
    assert (copied.actor_staff_id, copied.before) == (member.id, None)
    assert copied.after is not None
    assert (copied.after["copied_from_route_id"], copied.after["price_round_trip"]) == (
        str(original),
        2400,
    )
    assert entries["route.areas_changed"].after == {"served_area_ids": [str(a) for a in served]}
    # The original is untouched.
    after = (await api.get(route_path(original), headers=signed_in(member.id))).json()
    assert {**after, "updated_at": None} == {**source, "updated_at": None}


@pytest.mark.parametrize(
    ("prices", "status"),
    [
        (PRICED, "forming"),
        ({"price_round_trip": 2400, "price_one_way": 1400}, "forming"),
        ({"price_round_trip": 2400}, "draft"),  # not ready to form
        ({}, "draft"),
    ],
    ids=str,
)
async def test_an_individual_copy_forms_when_the_original_could(
    api: httpx.AsyncClient, db_session: AsyncSession, prices: dict[str, int], status: str
) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session, status="draft", **prices)
    response = await api.post(copies_path(original), json=VAN, headers=with_key(member_id))
    assert (response.status_code, response.json()["status"]) == (201, status)
    assert response.json()["served_areas"] == []


async def test_a_student_van_keeps_its_school(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(
        db_session, route_type="student", school_name="ICS", price_round_trip=2000,
        price_one_way=1200,
    )  # fmt: skip
    response = await api.post(copies_path(original), json=VAN, headers=with_key(member_id))
    assert (response.json()["route_type"], response.json()["school_name"]) == ("student", "ICS")


async def test_a_corporate_van_starts_as_a_draft(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    company = await add_company(db_session)
    original = await add_route(
        db_session, route_type="corporate", company_id=company, launch_minimum=None,
        status="active",
    )  # fmt: skip
    body = {"code": "ACME-02", "seats": 20}
    response = await api.post(copies_path(original), json=body, headers=with_key(member_id))
    copy = response.json()
    assert (response.status_code, copy["status"], copy["company"]["id"]) == (
        201,
        "draft",
        str(company),
    )
    assert (copy["launch_minimum"], copy["prices"]) == (None, None)
    with_minimum = await api.post(
        copies_path(original), json={**body, "code": "ACME-03", "launch_minimum": 5},
        headers=with_key(member_id),
    )  # fmt: skip
    assert [e["field"] for e in with_minimum.json()["errors"]] == ["/launch_minimum"]


async def test_a_corporate_route_of_an_inactive_company_is_not_copied(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    company = await add_company(db_session, is_active=False)
    original = await add_route(
        db_session, route_type="corporate", company_id=company, launch_minimum=None
    )
    response = await api.post(
        copies_path(original), json={"code": "ACME-02", "seats": 20}, headers=with_key(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_NOT_READY")


async def test_an_individual_van_needs_its_launch_minimum(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session)
    body = {"code": "AYAT-02", "seats": 12}
    response = await api.post(copies_path(original), json=body, headers=with_key(member_id))
    assert [e["field"] for e in response.json()["errors"]] == ["/launch_minimum"]


async def test_a_code_in_use_is_409(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session, code="AYAT-01")
    response = await api.post(
        copies_path(original), json={**VAN, "code": "ayat-01"}, headers=with_key(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_CODE_TAKEN")


async def test_a_copy_outlives_its_original(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The original may be deleted later: the copy still names it (a 404 if followed)."""
    admin_id = (await add_admin(db_session)).id
    original = await add_route(db_session)
    copy = (await api.post(copies_path(original), json=VAN, headers=with_key(admin_id))).json()
    assert (await api.delete(route_path(original), headers=signed_in(admin_id))).status_code == 204
    shown = await api.get(route_path(uuid.UUID(copy["id"])), headers=signed_in(admin_id))
    assert (shown.status_code, shown.json()["copied_from_route_id"]) == (200, str(original))
    again = await api.post(
        copies_path(uuid.UUID(copy["id"])), json={**VAN, "code": "AYAT-03"},
        headers=with_key(admin_id),
    )  # fmt: skip
    assert again.json()["copied_from_route_id"] == copy["id"]  # a copy of the copy


async def test_an_older_route_is_copied_as_it_is(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Made before arrival times existed: the copy has none either, until staff set both."""
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session, morning_arrival_time=None, return_arrival_time=None)
    copy = (await api.post(copies_path(original), json=VAN, headers=with_key(member_id))).json()
    assert (copy["morning_arrival_time"], copy["return_arrival_time"]) == (None, None)
    assert copy["morning_time"] == "06:30"


async def test_a_retried_copy_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session)
    headers = with_key(member_id)
    first = await api.post(copies_path(original), json=VAN, headers=headers)
    again = await api.post(copies_path(original), json=VAN, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert (again.json(), again.headers["Idempotent-Replayed"]) == (first.json(), "true")
    missing = await api.post(copies_path(original), json=VAN, headers=signed_in(member_id))
    assert missing.status_code == 428


@pytest.mark.parametrize(
    "change",
    [
        {"code": None},
        {"code": "A"},
        {"seats": 0},
        {"seats": 61},
        {"seats": "12"},
        {"launch_minimum": 13},  # above its seats
        {"launch_minimum": 0},
        {"price_round_trip": 1},  # prices are copied, never set here
        {"prices": {"round_trip": 1, "one_way": 1}},
        {"corridor_id": str(uuid.UUID(int=1))},
        {"status": "active"},
        {"surprise": 1},
    ],
    ids=str,
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, Any]
) -> None:
    member_id = (await add_staff(db_session)).id
    original = await add_route(db_session)
    body = {key: value for key, value in {**VAN, **change}.items() if value is not None}
    response = await api.post(copies_path(original), json=body, headers=with_key(member_id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    target = await add_route(db_session, deleted_at=START) if deleted else uuid.uuid4()
    response = await api.post(copies_path(target), json=VAN, headers=with_key(member_id))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    original = await add_route(db_session)
    assert (await api.post(copies_path(original), json=VAN)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        response = await api.post(copies_path(original), json=VAN, headers=with_key(caller))
        assert response.status_code == 403
    async with db_session.begin():
        copies = await db_session.scalar(
            select(func.count()).select_from(Route).where(Route.copied_from_route_id == original)
        )
    assert copies == 0


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.post(
        copies_path(uuid.UUID(int=1)), json=VAN, headers=with_key(uuid.uuid4())
    )
    assert response.status_code == 503


async def test_a_copy_waiting_on_an_areas_change_copies_the_new_list(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """The copy share-locks the original, so it sees the served areas whole: before or
    after a change, never half of one."""
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            original = await RouteFactory().create(session)
            area = await AreaFactory().create(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE", original.id)
            request = asyncio.create_task(
                committed_api.post(copies_path(original.id), json=VAN, headers=with_key(member.id))
            )
            await wait_for_lock_waiters(database)
            await conn.execute(
                "INSERT INTO gize.route_areas (route_id, area_id, position) VALUES ($1, $2, 1)",
                original.id,
                area.id,
            )
    finally:
        await conn.close()
    response = await request
    assert [a["id"] for a in response.json()["served_areas"]] == [str(area.id)]


async def _while_held(
    database: DatabaseUrls,
    lock: str,
    change: str,
    row_id: uuid.UUID,
    call: Coroutine[Any, Any, httpx.Response],
) -> httpx.Response:
    """Run `call` while another transaction holds `lock` on a row, then makes `change` to it
    and commits."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute(lock, row_id)
            request = asyncio.create_task(call)
            await wait_for_lock_waiters(database)
            await conn.execute(change, row_id)
    finally:
        await conn.close()
    return await request


async def test_a_copy_waiting_on_a_delete_finds_the_route_gone(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            original = await RouteFactory().create(session)
    response = await _while_held(
        database,
        "SELECT 1 FROM gize.routes WHERE id = $1 FOR UPDATE",
        "UPDATE gize.routes SET deleted_at = now() WHERE id = $1",
        original.id,
        committed_api.post(copies_path(original.id), json=VAN, headers=with_key(member.id)),
    )
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_a_corporate_copy_waiting_on_a_company_deactivation_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
        async with session.begin():
            company = await CompanyFactory().create(session)
            original = await RouteFactory().create(
                session, route_type="corporate", company_id=company.id, launch_minimum=None
            )
    response = await _while_held(
        database,
        "SELECT 1 FROM gize.companies WHERE id = $1 FOR UPDATE",
        "UPDATE gize.companies SET is_active = false WHERE id = $1",
        company.id,
        committed_api.post(
            copies_path(original.id),
            json={"code": "ACME-02", "seats": 20},
            headers=with_key(member.id),
        ),
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_NOT_READY")


def test_copies_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}/copies"]["post"]
    assert operation["operationId"] == "routes_copy"
    assert operation["responses"]["409"]["x-error-codes"] == [
        "IDEMPOTENCY_IN_PROGRESS",
        "ROUTE_CODE_TAKEN",
        "ROUTE_NOT_READY",
    ]
    assert operation["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]
    body = schema["components"]["schemas"]["RouteCopy"]
    assert set(body["properties"]) == {"code", "seats", "launch_minimum"}
    assert body["additionalProperties"] is False
