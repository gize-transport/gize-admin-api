"""GZ-012: GET /v1/audit-log, for admins only.

Entries get their time from the database (the app role cannot set it), so the rows these
tests list are seeded as the table's owner, committed, with chosen times, and deleted again
afterwards (by the owner: the app role cannot delete audit rows).
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from tests.support import DatabaseUrls, SettingsFactory
from tests.tokens import bearer, make_token

LOG = "/v1/audit-log"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


class Seed:
    """Committed staff and audit rows, written as the owner (who may set any column)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.ticks = 0
        self.staff_ids: list[uuid.UUID] = []
        self.entity_ids: list[uuid.UUID] = []

    async def remove(self) -> None:
        await self.conn.execute(
            "DELETE FROM gize.audit_log WHERE entity_id = ANY($1)", self.entity_ids
        )
        await self.conn.execute("DELETE FROM gize.staff WHERE id = ANY($1)", self.staff_ids)

    async def staff(self, role: str = "admin", *, active: bool = True) -> uuid.UUID:
        staff_id = uuid.uuid4()
        self.staff_ids.append(staff_id)
        await self.conn.execute(
            "INSERT INTO gize.staff (id, full_name, email, role, is_active)"
            " VALUES ($1, 'Abebe Kebede', $2, $3, $4)",
            staff_id,
            f"{staff_id}@example.com",
            role,
            active,
        )
        return staff_id

    async def entries(self, count: int, *, actor: uuid.UUID | None, entity_id: uuid.UUID) -> None:
        self.entity_ids.append(entity_id)
        for _ in range(count):
            self.ticks += 1
            await self.conn.execute(
                "INSERT INTO gize.audit_log (created_at, actor_staff_id, action, entity_type,"
                " entity_id, before, after) VALUES ($1, $2, 'thing.updated', 'thing', $3,"
                " $4::jsonb, $5::jsonb)",
                START + timedelta(seconds=self.ticks),
                actor,
                entity_id,
                json.dumps({"label": f"v{self.ticks - 1}", "name": "[redacted]"}),
                json.dumps({"label": f"v{self.ticks}", "name": "[redacted]"}),
            )


@pytest.fixture
async def seed(database: DatabaseUrls) -> AsyncIterator[Seed]:
    conn = await asyncpg.connect(database.migrator, timeout=3)
    seeded = Seed(conn)
    try:
        yield seeded
    finally:
        try:
            await seeded.remove()
        finally:
            await conn.close()


def _as(staff_id: uuid.UUID) -> dict[str, str]:
    return bearer(make_token(sub=str(staff_id)))


async def test_admins_read_the_log_newest_first(api: httpx.AsyncClient, seed: Seed) -> None:
    admin = await seed.staff()
    thing = uuid.uuid4()
    await seed.entries(3, actor=admin, entity_id=thing)
    response = await api.get(LOG, headers=_as(admin))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert set(first) == {
        "id", "created_at", "actor_staff_id", "action", "entity_type", "entity_id",
        "before", "after", "request_id",
    }  # fmt: skip
    assert first["actor_staff_id"] == str(admin)
    assert (first["entity_type"], first["entity_id"]) == ("thing", str(thing))
    assert [item["after"]["label"] for item in body["items"]] == ["v3", "v2", "v1"]


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, seed: Seed) -> None:
    admin = await seed.staff()
    await seed.entries(5, actor=admin, entity_id=uuid.uuid4())
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(LOG, params=params, headers=_as(admin))).json()
        seen += [item["after"]["label"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert seen == ["v5", "v4", "v3", "v2", "v1"]
    assert cursor is None


async def test_filters_by_entity_and_actor(api: httpx.AsyncClient, seed: Seed) -> None:
    admin = await seed.staff()
    other = await seed.staff()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await seed.entries(2, actor=admin, entity_id=mine)
    await seed.entries(1, actor=other, entity_id=theirs)
    await seed.entries(1, actor=None, entity_id=theirs)

    async def found(**params: str) -> tuple[set[str], int]:
        body = (await api.get(LOG, params=params, headers=_as(admin))).json()
        return {item["entity_id"] for item in body["items"]}, len(body["items"])

    assert await found(entity_type="thing", entity_id=str(theirs)) == ({str(theirs)}, 2)
    assert await found(actor_staff_id=str(admin)) == ({str(mine)}, 2)
    assert await found(entity_type="thing", entity_id=str(theirs), actor_staff_id=str(other)) == (
        {str(theirs)},
        1,
    )
    assert await found(entity_type="route", entity_id=str(theirs)) == (set(), 0)


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, seed: Seed, db_session: AsyncSession
) -> None:
    """No N+1: one query for the caller, one for the page (engineering-standards.md)."""
    admin = await seed.staff()
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        await seed.entries(rows, actor=admin, entity_id=uuid.uuid4())
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(LOG, headers=_as(admin))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts == [2, 2]


async def test_staff_who_are_not_admins_get_403(api: httpx.AsyncClient, seed: Seed) -> None:
    staff = await seed.staff(role="staff")
    response = await api.get(LOG, headers=_as(staff))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "params", [{"surprise": "1"}, {"limit": "0"}, {"cursor": "garbage"}, {"entity_id": "x"}]
)
async def test_the_role_is_checked_before_the_parameters(
    api: httpx.AsyncClient, seed: Seed, params: dict[str, str]
) -> None:
    """A non-admin learns nothing from how their parameters would have been judged."""
    staff = await seed.staff(role="staff")
    assert (await api.get(LOG, params=params, headers=_as(staff))).status_code == 403
    assert (await api.get(LOG, params=params)).status_code == 401


async def test_a_deactivated_admin_gets_403(api: httpx.AsyncClient, seed: Seed) -> None:
    admin = await seed.staff(active=False)
    assert (await api.get(LOG, headers=_as(admin))).status_code == 403


async def test_no_token_is_401(api: httpx.AsyncClient) -> None:
    assert (await api.get(LOG)).status_code == 401


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"entity_type": "thing", "entity_id": "not-a-uuid"}, "VALIDATION_FAILED"),
        ({"actor_staff_id": "nope"}, "VALIDATION_FAILED"),
        ({"entity_type": "Thing; DROP", "entity_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        ({"entity_type": "x" * 51, "entity_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        ({"entity_type": "thing"}, "VALIDATION_FAILED"),  # an entity is a type and an ID
        ({"entity_id": str(uuid.UUID(int=1))}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_parameters_are_422(
    api: httpx.AsyncClient, seed: Seed, params: dict[str, str], code: str
) -> None:
    admin = await seed.staff()
    response = await api.get(LOG, params=params, headers=_as(admin))
    assert response.status_code == 422
    assert response.json()["code"] == code


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    assert (await api.get(LOG, headers=bearer(make_token()))).status_code == 503


def test_the_log_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][LOG]["get"]
    assert operation["operationId"] == "audit_log_list"
    assert operation["responses"]["422"]["x-error-codes"] == ["INVALID_CURSOR", "VALIDATION_FAILED"]
    names = {parameter["name"] for parameter in operation["parameters"]}
    assert names == {"limit", "cursor", "entity_type", "entity_id", "actor_staff_id"}
    page = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert page == {"$ref": "#/components/schemas/AuditEntryPage"}  # a name clients keep
