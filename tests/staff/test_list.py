"""GZ-013: GET /v1/staff and GET /v1/staff/{staff_id}, for admins only."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.staff.models import Staff
from tests.factories import StaffFactory
from tests.staff.support import STAFF, add_admin, add_staff, signed_in
from tests.support import SettingsFactory

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def _staff_over_time(session: AsyncSession, count: int, **values: object) -> list[Staff]:
    """Members created a minute apart, oldest first (in one transaction `now()` is fixed)."""
    async with session.begin():
        return [
            await StaffFactory().create(
                session, created_at=START + timedelta(minutes=number), **values
            )
            for number in range(count)
        ]


async def test_admins_list_staff_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session, created_at=START - timedelta(days=1))
    members = await _staff_over_time(db_session, 3)
    response = await api.get(STAFF, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["next_cursor"] is None
    ids = [item["id"] for item in body["items"]]
    assert ids == [str(member.id) for member in reversed(members)] + [str(admin.id)]
    newest = members[-1]
    assert body["items"][0] == {
        "id": str(newest.id),
        "full_name": newest.full_name,
        "email": newest.email,
        "role": "staff",
        "is_active": True,
        "first_signed_in_at": None,
        "created_at": "2026-10-29T06:32:00Z",
        "updated_at": body["items"][0]["updated_at"],
    }


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session, created_at=START - timedelta(days=1))
    members = await _staff_over_time(db_session, 4)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(STAFF, params=params, headers=signed_in(admin.id))).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert seen == [str(member.id) for member in reversed(members)] + [str(admin.id)]
    assert cursor is None


async def test_filters_by_role_and_status(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    gone = await add_staff(db_session, is_active=False)
    former_admin = await add_admin(db_session, is_active=False)
    member = await add_staff(db_session)

    async def found(**params: str) -> set[uuid.UUID]:
        response = await api.get(STAFF, params=params, headers=signed_in(admin.id))
        return {uuid.UUID(item["id"]) for item in response.json()["items"]}

    assert await found(role="admin") == {admin.id, former_admin.id}
    assert await found(is_active="false") == {gone.id, former_admin.id}
    assert await found(role="staff", is_active="true") == {member.id}


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """No N+1: one query for the caller, one for the page (engineering-standards.md)."""
    admin = await add_admin(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        await _staff_over_time(db_session, rows)
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(STAFF, headers=signed_in(admin.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts == [2, 2]


async def test_an_admin_reads_one_member(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session, full_name="Almaz Tesfaye", is_active=False)
    response = await api.get(f"{STAFF}/{member.id}", headers=signed_in(admin.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert (body["id"], body["full_name"], body["is_active"]) == (
        str(member.id),
        "Almaz Tesfaye",
        False,  # deactivated members are still shown: they can be reactivated
    )


async def test_an_unknown_member_is_404(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    response = await api.get(f"{STAFF}/{uuid.uuid4()}", headers=signed_in(admin.id))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.parametrize("path", [STAFF, f"{STAFF}/{{member}}"])
async def test_staff_who_are_not_admins_get_403(
    api: httpx.AsyncClient, db_session: AsyncSession, path: str
) -> None:
    member = await add_staff(db_session)
    response = await api.get(path.format(member=member.id), headers=signed_in(member.id))
    assert response.status_code == 403  # even their own record: the portal uses /me


@pytest.mark.parametrize("path", [STAFF, f"{STAFF}/{uuid.uuid4()}"])
async def test_a_deactivated_admin_gets_403(
    api: httpx.AsyncClient, db_session: AsyncSession, path: str
) -> None:
    admin = await add_admin(db_session, is_active=False)
    assert (await api.get(path, headers=signed_in(admin.id))).status_code == 403


@pytest.mark.parametrize("path", [STAFF, f"{STAFF}/{uuid.uuid4()}"])
async def test_no_token_is_401(api: httpx.AsyncClient, path: str) -> None:
    assert (await api.get(path)).status_code == 401


@pytest.mark.parametrize(
    "params", [{"surprise": "1"}, {"limit": "0"}, {"cursor": "garbage"}, {"role": "owner"}]
)
async def test_the_role_is_checked_before_the_parameters(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str]
) -> None:
    member = await add_staff(db_session)
    assert (await api.get(STAFF, params=params, headers=signed_in(member.id))).status_code == 403
    assert (await api.get(STAFF, params=params)).status_code == 401


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"role": "owner"}, "VALIDATION_FAILED"),
        ({"is_active": "maybe"}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    admin = await add_admin(db_session)
    response = await api.get(STAFF, params=params, headers=signed_in(admin.id))
    assert response.status_code == 422
    assert response.json()["code"] == code


async def test_a_malformed_member_id_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    response = await api.get(f"{STAFF}/not-a-uuid", headers=signed_in(admin.id))
    assert response.status_code == 422


@pytest.mark.parametrize("path", [STAFF, f"{STAFF}/{uuid.uuid4()}"])
async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession, path: str
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    assert (await api.get(path, headers=signed_in(uuid.uuid4()))).status_code == 503


def test_staff_management_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection, member = schema["paths"][STAFF], schema["paths"][f"{STAFF}/{{staff_id}}"]
    assert collection["get"]["operationId"] == "staff_list"
    assert collection["post"]["operationId"] == "staff_invite"
    assert member["get"]["operationId"] == "staff_get"
    assert member["patch"]["operationId"] == "staff_update"

    invite = collection["post"]["responses"]
    assert invite["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StaffRead"
    }
    assert invite["409"]["x-error-codes"] == [
        "AUTH_ACCOUNT_EXISTS",
        "IDEMPOTENCY_IN_PROGRESS",
        "STAFF_EMAIL_TAKEN",
    ]
    assert invite["422"]["x-error-codes"] == ["IDEMPOTENCY_KEY_MISMATCH", "VALIDATION_FAILED"]
    assert invite["428"]["x-error-codes"] == ["IDEMPOTENCY_KEY_REQUIRED"]
    key = {p["name"]: p for p in collection["post"]["parameters"]}["Idempotency-Key"]
    # Required, a UUID: generated clients must send it (a missing one is still a 428).
    assert (key["in"], key["required"]) == ("header", True)
    assert key["schema"] == {"type": "string", "format": "uuid"}
    assert set(invite["201"]["headers"]) == {"Location", "Idempotent-Replayed"}

    page = collection["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert page == {"$ref": "#/components/schemas/StaffPage"}
    names = {parameter["name"] for parameter in collection["get"]["parameters"]}
    assert names == {"limit", "cursor", "role", "is_active"}

    update = member["patch"]["responses"]
    assert update["404"]["x-error-codes"] == ["NOT_FOUND"]
    assert update["409"]["x-error-codes"] == ["LAST_ACTIVE_ADMIN"]
    assert member["get"]["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]

    schemas = schema["components"]["schemas"]
    changes = schemas["StaffUpdate"]
    assert changes["additionalProperties"] is False
    assert changes["minProperties"] == 1
    # Both optional, neither nullable: leave a field out to keep it.
    role, is_active = changes["properties"]["role"], changes["properties"]["is_active"]
    assert role["$ref"] == "#/components/schemas/StaffRole"
    assert is_active["type"] == "boolean"
    for field in (role, is_active):
        assert "anyOf" not in field  # no null alternative
        assert "default" not in field
    assert "required" not in changes
    assert set(schemas["StaffInvite"]["required"]) == {"email", "full_name", "role"}
    assert set(schemas["StaffRead"]["properties"]) == {
        "id", "full_name", "email", "role", "is_active", "first_signed_in_at", "created_at",
        "updated_at",
    }  # fmt: skip
    assert {"STAFF_EMAIL_TAKEN", "AUTH_ACCOUNT_EXISTS", "LAST_ACTIVE_ADMIN"} <= set(
        schemas["ErrorCode"]["enum"]
    )
