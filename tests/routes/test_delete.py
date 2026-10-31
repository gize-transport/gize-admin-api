"""GZ-024: admins delete routes (soft), never while a rider is reserved or riding.

Today is 15 November 2026 (conftest.py).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.routes.models import AssignmentStatus, Route
from tests.routes.support import ROUTES, add_route, add_seat, route_path
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

ACTIVE, RESERVED, ENDED = AssignmentStatus.ACTIVE, AssignmentStatus.RESERVED, AssignmentStatus.ENDED
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def test_admins_delete_a_route(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin_id = (await add_admin(db_session)).id  # a refused request expires loaded rows
    route = await add_route(db_session, code="BOLE-01", status="active")
    response = await api.delete(route_path(route), headers=signed_in(admin_id))
    assert response.status_code == 204
    assert response.content == b""
    gone = await api.get(route_path(route), headers=signed_in(admin_id))
    assert gone.status_code == 404
    listed = await api.get(ROUTES, headers=signed_in(admin_id))
    assert route not in {uuid.UUID(item["id"]) for item in listed.json()["items"]}
    async with db_session.begin():
        assert (await db_session.get_one(Route, route)).deleted_at is not None
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.actor_staff_id) == ("route.deleted", admin_id)
    assert entry.before == {"deleted_at": None}
    assert entry.after is not None
    assert entry.after["deleted_at"] is not None


async def test_a_deleted_routes_code_is_free_again(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    route = await add_route(db_session, code="BOLE-01")
    async with db_session.begin():
        corridor_id = (await db_session.get_one(Route, route)).corridor_id
    await api.delete(route_path(route), headers=signed_in(admin_id))
    body = {
        "code": "BOLE-01",
        "corridor_id": str(corridor_id),
        "route_type": "public",
        "morning_time": "06:30",
        "morning_arrival_time": "07:15",
        "return_time": "17:30",
        "return_arrival_time": "18:15",
        "seats": 14,
        "launch_minimum": 10,
    }
    response = await api.post(ROUTES, json=body, headers=with_key(admin_id))
    assert response.status_code == 201


@pytest.mark.parametrize(
    ("seat", "why"),
    [
        ({"status": RESERVED}, "reserved for December"),
        ({"status": RESERVED, "start_date": date(2027, 6, 1)}, "reserved far ahead"),
        ({"status": ACTIVE, "start_date": date(2026, 9, 1)}, "riding"),
        (
            {"status": ACTIVE, "start_date": date(2026, 9, 1), "end_date": date(2026, 11, 15)},
            "last day today",
        ),
    ],
)
async def test_a_route_with_riders_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession, seat: dict[str, object], why: str
) -> None:
    admin_id = (await add_admin(db_session)).id
    route = await add_route(db_session, status="forming")
    await add_seat(db_session, route, **seat)
    response = await api.delete(route_path(route), headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_RIDERS"), why
    assert (await api.get(route_path(route), headers=signed_in(admin_id))).status_code == 200
    assert await audit_entries(db_session, route) == []


async def test_riders_who_have_gone_dont_keep_a_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    route = await add_route(db_session, status="active")
    await add_seat(db_session, route, status=ENDED)
    await add_seat(
        db_session, route, status=ACTIVE, start_date=date(2026, 9, 1), end_date=date(2026, 10, 31)
    )
    response = await api.delete(route_path(route), headers=signed_in(admin_id))
    assert response.status_code == 204


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    admin_id = (await add_admin(db_session)).id
    target = await add_route(db_session, deleted_at=START) if deleted else uuid.uuid4()
    response = await api.delete(route_path(target), headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_staff_cannot_delete_routes(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    gone_id = (await add_admin(db_session, is_active=False)).id
    route = await add_route(db_session)
    assert (await api.delete(route_path(route))).status_code == 401
    for caller in (member_id, gone_id, uuid.uuid4()):
        response = await api.delete(route_path(route), headers=signed_in(caller))
        assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert (await api.get(route_path(route), headers=signed_in(member_id))).status_code == 200


async def test_an_invalid_id_is_422(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin_id = (await add_admin(db_session)).id
    response = await api.delete(f"{ROUTES}/not-a-uuid", headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.delete(route_path(uuid.UUID(int=1)), headers=signed_in(uuid.uuid4()))
    assert response.status_code == 503


def test_delete_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}"]["delete"]
    assert operation["operationId"] == "routes_delete"
    assert operation["responses"]["409"]["x-error-codes"] == ["ROUTE_HAS_RIDERS"]
    assert operation["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]
    assert "204" in operation["responses"]
