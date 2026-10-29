"""GZ-012: GET /v1/audit-log, for admins only."""

import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED, AuditedEntity, record
from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.support import SettingsFactory
from tests.tokens import bearer, make_token

LOG = "/v1/audit-log"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
_clock = itertools.count()
THING = AuditedEntity(type="thing", fields=("label", "name"), visible=frozenset({"label"}))


async def _staff(
    db_session: AsyncSession, role: StaffRole = StaffRole.ADMIN, **kw: object
) -> Staff:
    async with db_session.begin():
        return await StaffFactory().create(db_session, role=role, **kw)


def _as(staff: Staff) -> dict[str, str]:
    return bearer(make_token(sub=str(staff.id)))


async def _log(
    db_session: AsyncSession, count: int, *, actor: Staff | None, entity_id: uuid.UUID
) -> None:
    async with db_session.begin():
        for number in range(count):
            entry = record(
                db_session,
                actor_id=actor.id if actor else None,
                action="thing.updated",
                entity=THING,
                entity_id=entity_id,
                before={"label": f"v{number}", "name": "Abebe"},
                after={"label": f"v{number + 1}", "name": "Kebede"},
            )
            # Rows of one transaction share now(); give each its own time to order by.
            assert entry is not None
            entry.created_at = START + timedelta(seconds=next(_clock))


async def test_admins_read_the_log_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _staff(db_session)
    thing = uuid.uuid4()
    await _log(db_session, 3, actor=admin, entity_id=thing)
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
    assert first["actor_staff_id"] == str(admin.id)
    assert (first["entity_type"], first["entity_id"]) == ("thing", str(thing))
    assert first["before"]["name"] == REDACTED
    assert [item["after"]["label"] for item in body["items"]] == ["v3", "v2", "v1"]
    assert "Abebe" not in response.text


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await _staff(db_session)
    await _log(db_session, 5, actor=admin, entity_id=uuid.uuid4())
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(LOG, params=params, headers=_as(admin))).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert len(seen) == len(set(seen)) == 5
    assert cursor is None


async def test_filters_by_entity_and_actor(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _staff(db_session)
    other = await _staff(db_session)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _log(db_session, 2, actor=admin, entity_id=mine)
    await _log(db_session, 1, actor=other, entity_id=theirs)
    await _log(db_session, 1, actor=None, entity_id=theirs)

    async def ids(**params: str) -> tuple[set[str], int]:
        body = (await api.get(LOG, params=params, headers=_as(admin))).json()
        return {item["entity_id"] for item in body["items"]}, len(body["items"])

    assert await ids(entity_type="thing", entity_id=str(theirs)) == ({str(theirs)}, 2)
    assert await ids(actor_staff_id=str(admin.id)) == ({str(mine)}, 2)
    assert await ids(entity_type="route") == (set(), 0)


async def test_staff_who_are_not_admins_get_403(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session, role=StaffRole.STAFF)
    response = await api.get(LOG, headers=_as(staff))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_a_deactivated_admin_gets_403(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _staff(db_session, is_active=False)
    assert (await api.get(LOG, headers=_as(admin))).status_code == 403


async def test_no_token_is_401(api: httpx.AsyncClient) -> None:
    assert (await api.get(LOG)).status_code == 401


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"entity_id": "not-a-uuid"}, "VALIDATION_FAILED"),
        ({"actor_staff_id": "nope"}, "VALIDATION_FAILED"),
        ({"entity_type": "Thing; DROP"}, "VALIDATION_FAILED"),
        ({"entity_type": "x" * 51}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    admin = await _staff(db_session)
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
    operation = create_app(make_settings()).openapi()["paths"][LOG]["get"]
    assert operation["operationId"] == "audit_log_list"
    assert operation["responses"]["422"]["x-error-codes"] == ["INVALID_CURSOR", "VALIDATION_FAILED"]
    names = {parameter["name"] for parameter in operation["parameters"]}
    assert names == {"limit", "cursor", "entity_type", "entity_id", "actor_staff_id"}
