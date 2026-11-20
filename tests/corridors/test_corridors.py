"""GZ-020: corridors, each a pair of areas (from, to).

Any active staff member manages them; only admins delete (soft delete). An area a live
corridor uses cannot be deleted. Parallel requests are in test_concurrency.py.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.areas.models import Area
from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode
from gize_api.corridors import service
from gize_api.corridors.models import Corridor
from gize_api.main import GizeAPI, create_app
from tests.factories import AreaFactory, CorridorFactory
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

CORRIDORS = "/v1/corridors"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def _areas(session: AsyncSession, *names: str) -> list[uuid.UUID]:
    async with session.begin():
        return [(await AreaFactory().create(session, name=name)).id for name in names]


async def _corridor(session: AsyncSession, **values: object) -> uuid.UUID:
    async with session.begin():
        return (await CorridorFactory().create(session, **values)).id


def _member(corridor_id: uuid.UUID) -> str:
    return f"{CORRIDORS}/{corridor_id}"


def _pair(origin: uuid.UUID, destination: uuid.UUID) -> dict[str, str]:
    return {"origin_area_id": str(origin), "destination_area_id": str(destination)}


# Create


async def test_staff_create_a_corridor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    response = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=with_key(member.id))
    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "origin_area": {"id": str(ayat), "name": "Ayat"},
        "destination_area": {"id": str(mexico), "name": "Mexico"},
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert response.headers["Location"] == f"{CORRIDORS}/{body['id']}"
    (entry,) = await audit_entries(db_session, uuid.UUID(body["id"]))
    assert (entry.action, entry.actor_staff_id, entry.before) == (
        "corridor.created",
        member.id,
        None,
    )
    assert entry.after == {
        "origin_area_id": str(ayat),
        "destination_area_id": str(mexico),
        "deleted_at": None,
    }


async def test_a_retried_create_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    headers = with_key(member.id)
    first = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=headers)
    again = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    missing = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=signed_in(member.id))
    assert missing.status_code == 428


async def test_a_pair_in_use_is_409_but_the_way_back_is_another_corridor(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id  # a refused request expires loaded rows
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    again = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=with_key(member_id))
    assert (again.status_code, again.json()["code"]) == (409, "CORRIDOR_EXISTS")
    back = await api.post(CORRIDORS, json=_pair(mexico, ayat), headers=with_key(member_id))
    assert back.status_code == 201


async def test_a_deleted_corridors_pair_can_be_used_again(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico, deleted_at=START)
    response = await api.post(CORRIDORS, json=_pair(ayat, mexico), headers=with_key(member.id))
    assert response.status_code == 201


@pytest.mark.parametrize("which", ["origin_area_id", "destination_area_id"])
@pytest.mark.parametrize("missing", ["unknown", "deleted"])
async def test_an_area_that_does_not_exist_is_422_on_its_field(
    api: httpx.AsyncClient, db_session: AsyncSession, which: str, missing: str
) -> None:
    member = await add_staff(db_session)
    (ayat,) = await _areas(db_session, "Ayat")
    if missing == "unknown":
        gone = uuid.uuid4()
    else:
        async with db_session.begin():
            gone = (await AreaFactory().create(db_session, deleted_at=START)).id
    body = {"origin_area_id": str(ayat), "destination_area_id": str(ayat), which: str(gone)}
    response = await api.post(CORRIDORS, json=body, headers=with_key(member.id))
    problem = response.json()
    assert (response.status_code, problem["code"]) == (422, "VALIDATION_FAILED")
    assert [(e["location"], e["field"]) for e in problem["errors"]] == [("body", f"/{which}")]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"origin_area_id": str(uuid.UUID(int=1))},
        {"origin_area_id": "nope", "destination_area_id": str(uuid.UUID(int=2))},
        {"origin_area_id": str(uuid.UUID(int=1)), "destination_area_id": str(uuid.UUID(int=1))},
        {
            "origin_area_id": str(uuid.UUID(int=1)),
            "destination_area_id": str(uuid.UUID(int=2)),
            "name": "Ayat to Mexico",
        },
        [],
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member = await add_staff(db_session)
    response = await api.post(CORRIDORS, json=body, headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# List and read


async def test_staff_list_corridors_newest_first_with_area_names(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    ayat, mexico, bole = await _areas(db_session, "Ayat", "Mexico", "Bole")
    older = await _corridor(
        db_session, origin_area_id=ayat, destination_area_id=mexico, created_at=START
    )
    newer = await _corridor(
        db_session, origin_area_id=bole, destination_area_id=mexico, created_at=START + timedelta(1)
    )
    await _corridor(db_session, deleted_at=START, created_at=START + timedelta(2))
    response = await api.get(CORRIDORS, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(newer), str(older)]
    assert [(i["origin_area"]["name"], i["destination_area"]["name"]) for i in items] == [
        ("Bole", "Mexico"),
        ("Ayat", "Mexico"),
    ]


async def test_filters_by_either_end(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    ayat, mexico, bole = await _areas(db_session, "Ayat", "Mexico", "Bole")
    to_mexico = await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    from_mexico = await _corridor(db_session, origin_area_id=mexico, destination_area_id=bole)

    async def found(**params: uuid.UUID) -> set[str]:
        query = {name: str(value) for name, value in params.items()}
        response = await api.get(CORRIDORS, params=query, headers=signed_in(member.id))
        return {item["id"] for item in response.json()["items"]}

    assert await found(destination_area_id=mexico) == {str(to_mexico)}
    assert await found(origin_area_id=mexico) == {str(from_mexico)}
    assert await found(origin_area_id=ayat, destination_area_id=bole) == set()


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    made = [await _corridor(db_session, created_at=START + timedelta(minutes=i)) for i in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(CORRIDORS, params=params, headers=signed_in(member.id))).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert (seen, cursor) == ([str(c) for c in reversed(made)], None)


@pytest.mark.parametrize("sort", ["newest", "name"])
async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession, sort: str
) -> None:
    """Area names come with the page, not one query per corridor (sorted by name too:
    GZ-082)."""
    member = await add_staff(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        async with db_session.begin():
            await CorridorFactory().create_batch(db_session, rows)
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            response = await api.get(CORRIDORS, params={"sort": sort}, headers=signed_in(member.id))
            assert response.status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts == [2, 2]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"origin_area_id": "nope"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_list_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    member = await add_staff(db_session)
    response = await api.get(CORRIDORS, params=params, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_staff_read_one_corridor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    corridor = await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    response = await api.get(_member(corridor), headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.json()["destination_area"] == {"id": str(mexico), "name": "Mexico"}


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_corridors_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    admin_id = (await add_admin(db_session)).id
    (area,) = await _areas(db_session, "Ayat")
    target = await _corridor(db_session, deleted_at=START) if deleted else uuid.uuid4()
    for method in ("GET", "PATCH", "DELETE"):
        response = await api.request(
            method,
            _member(target),
            json={"origin_area_id": str(area)},
            headers=signed_in(admin_id),
        )
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), method


# Edit


async def test_staff_change_an_end(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    ayat, mexico, piassa = await _areas(db_session, "Ayat", "Mexico", "Piassa")
    corridor = await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    body = {"destination_area_id": str(piassa)}
    response = await api.patch(_member(corridor), json=body, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.json()["destination_area"]["name"] == "Piassa"
    (entry,) = await audit_entries(db_session, corridor)
    assert (entry.action, entry.before, entry.after) == (
        "corridor.updated",
        {"destination_area_id": str(mexico)},
        {"destination_area_id": str(piassa)},
    )


async def test_an_edit_to_what_is_there_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    corridor = await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    response = await api.patch(
        _member(corridor), json=_pair(ayat, mexico), headers=signed_in(member.id)
    )
    assert response.status_code == 200
    assert await audit_entries(db_session, corridor) == []


async def test_an_edit_into_a_pair_in_use_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    ayat, mexico, bole = await _areas(db_session, "Ayat", "Mexico", "Bole")
    await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    corridor = await _corridor(db_session, origin_area_id=bole, destination_area_id=mexico)
    body = {"origin_area_id": str(ayat)}
    response = await api.patch(_member(corridor), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (409, "CORRIDOR_EXISTS")


@pytest.mark.parametrize(
    "change", ["same_ends", "unknown_area", "deleted_area", "empty", "null", "unknown_field"]
)
async def test_invalid_edits_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: str
) -> None:
    member = await add_staff(db_session)
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    corridor = await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)
    async with db_session.begin():
        gone = (await AreaFactory().create(db_session, deleted_at=START)).id
    body: object = {
        "same_ends": {"destination_area_id": str(ayat)},
        "unknown_area": {"origin_area_id": str(uuid.uuid4())},
        "deleted_area": {"destination_area_id": str(gone)},
        "empty": {},
        "null": {"origin_area_id": None},
        "unknown_field": {"name": "Ayat to Mexico"},
    }[change]
    response = await api.patch(_member(corridor), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# Delete


async def test_admins_delete_a_corridor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    corridor = await _corridor(db_session)
    response = await api.delete(_member(corridor), headers=signed_in(admin.id))
    assert (response.status_code, response.content) == (204, b"")
    async with db_session.begin():
        assert (await db_session.get_one(Corridor, corridor)).deleted_at is not None
    (entry,) = await audit_entries(db_session, corridor)
    assert (entry.action, entry.actor_staff_id) == ("corridor.deleted", admin.id)


async def test_staff_cannot_delete(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    corridor = await _corridor(db_session)
    assert (await api.delete(_member(corridor), headers=signed_in(member.id))).status_code == 403


# Areas in use


@pytest.mark.parametrize("end", ["origin_area_id", "destination_area_id"])
async def test_an_area_a_corridor_uses_cannot_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession, end: str
) -> None:
    admin_id = (await add_admin(db_session)).id
    (area,) = await _areas(db_session, "Ayat")
    await _corridor(db_session, **{end: area})
    response = await api.delete(f"/v1/areas/{area}", headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (409, "AREA_IN_USE")
    async with db_session.begin():
        assert (await db_session.get_one(Area, area)).deleted_at is None


async def test_an_area_only_deleted_corridors_used_can_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    (area,) = await _areas(db_session, "Ayat")
    await _corridor(db_session, origin_area_id=area, deleted_at=START)
    response = await api.delete(f"/v1/areas/{area}", headers=signed_in(admin_id))
    assert response.status_code == 204


# Who may call


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", CORRIDORS),
        ("GET", CORRIDORS),
        ("GET", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
        ("DELETE", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
    ],
)
async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    body = _pair(uuid.UUID(int=1), uuid.UUID(int=2))
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert (await api.request(method, path, json=body, headers=key)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        headers = {**signed_in(caller), **key}
        assert (await api.request(method, path, json=body, headers=headers)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", CORRIDORS),
        ("GET", CORRIDORS),
        ("GET", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
        ("DELETE", f"{CORRIDORS}/{uuid.UUID(int=1)}"),
    ],
)
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    method: str,
    path: str,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    body = _pair(uuid.UUID(int=1), uuid.UUID(int=2))
    response = await api.request(method, path, json=body, headers=with_key(uuid.uuid4()))
    assert response.status_code == 503


def test_corridors_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection = schema["paths"][CORRIDORS]
    member = schema["paths"][f"{CORRIDORS}/{{corridor_id}}"]
    assert {op["operationId"] for op in collection.values()} == {
        "corridors_create",
        "corridors_list",
    }
    assert {op["operationId"] for op in member.values()} == {
        "corridors_get",
        "corridors_update",
        "corridors_delete",
    }
    assert collection["post"]["responses"]["409"]["x-error-codes"] == [
        "CORRIDOR_EXISTS",
        "IDEMPOTENCY_IN_PROGRESS",
    ]
    assert member["patch"]["responses"]["409"]["x-error-codes"] == [
        "CORRIDOR_EXISTS",
        "CORRIDOR_IN_USE",  # GZ-021: routes run on it
    ]
    area_delete = schema["paths"]["/v1/areas/{area_id}"]["delete"]
    assert area_delete["responses"]["409"]["x-error-codes"] == ["AREA_IN_USE"]
    update = schema["components"]["schemas"]["CorridorUpdate"]
    assert (update["additionalProperties"], update["minProperties"]) == (False, 1)
    assert "anyOf" not in update["properties"]["origin_area_id"]


async def test_other_constraint_errors_are_not_reported_as_a_pair_taken(
    db_session: AsyncSession,
) -> None:
    """Only the pair index's violation becomes CORRIDOR_EXISTS; the table's own checks (two
    different areas) stay errors: a bug the schema should have caught."""
    ayat, mexico = await _areas(db_session, "Ayat", "Mexico")
    await _corridor(db_session, origin_area_id=ayat, destination_area_id=mexico)

    async def flush_new(origin: uuid.UUID, destination: uuid.UUID) -> None:
        async with db_session.begin():
            db_session.add(Corridor(origin_area_id=origin, destination_area_id=destination))
            await service._flush(db_session)

    with pytest.raises(IntegrityError):
        await flush_new(ayat, ayat)
    with pytest.raises(AppError) as taken:
        await flush_new(ayat, mexico)
    assert taken.value.code is ErrorCode.CORRIDOR_EXISTS
