"""GZ-026: areas, the staff-managed list of neighbourhoods and landmarks.

Any active staff member manages them; only admins delete (soft delete). Parallel creates
are in test_concurrency.py.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.areas import service
from gize_api.areas.models import Area
from gize_api.core.db import get_session, violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.main import GizeAPI, create_app
from tests.factories import AreaFactory
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

AREAS = "/v1/areas"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def _area(session: AsyncSession, **values: object) -> Area:
    async with session.begin():
        return await AreaFactory().create(session, **values)


async def _reread(session: AsyncSession, area_id: uuid.UUID) -> Area:
    async with session.begin():
        return await session.get_one(Area, area_id)


async def _actions(session: AsyncSession, area_id: uuid.UUID) -> list[tuple[str, object, object]]:
    return [(e.action, e.before, e.after) for e in await audit_entries(session, area_id)]


def _member(area: Area | uuid.UUID) -> str:
    return f"{AREAS}/{area.id if isinstance(area, Area) else area}"


# Create


async def test_staff_create_an_area(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    response = await api.post(AREAS, json={"name": "  Ayat  "}, headers=with_key(member.id))
    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "name": "Ayat",  # trimmed; case kept as typed
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert response.headers["Location"] == f"{AREAS}/{body['id']}"
    area_id = uuid.UUID(body["id"])
    assert (await _reread(db_session, area_id)).name == "Ayat"
    (entry,) = await audit_entries(db_session, area_id)
    assert (entry.action, entry.actor_staff_id) == ("area.created", member.id)
    assert (entry.before, entry.after) == (None, {"name": "Ayat", "deleted_at": None})


async def test_a_retried_create_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    headers = with_key(member.id)
    first = await api.post(AREAS, json={"name": "Ayat"}, headers=headers)
    again = await api.post(AREAS, json={"name": "Ayat"}, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    other = await api.post(AREAS, json={"name": "Bole"}, headers=headers)
    assert (other.status_code, other.json()["code"]) == (422, "IDEMPOTENCY_KEY_MISMATCH")


async def test_create_needs_an_idempotency_key(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    response = await api.post(AREAS, json={"name": "Ayat"}, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")


@pytest.mark.parametrize(
    "name",
    [
        "Ayat CMC",
        "AYAT CMC",
        " ayat cmc ",
        "Ayat\u00a0CMC",  # a non-breaking space
        "Ayat   CMC",  # repeated spaces
    ],
)
async def test_a_name_in_use_is_409_ignoring_case(
    api: httpx.AsyncClient, db_session: AsyncSession, name: str
) -> None:
    member = await add_staff(db_session)
    await _area(db_session, name="Ayat CMC")
    response = await api.post(AREAS, json={"name": name}, headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (409, "AREA_NAME_TAKEN")


async def test_names_are_stored_the_way_they_look(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """One ordinary space between words, and Unicode NFC: "é" typed as e + accent is "é"."""
    member = await add_staff(db_session)
    body = {"name": "Caf\u0065\u0301\u00a0\u00a0Bole  Road"}
    response = await api.post(AREAS, json=body, headers=with_key(member.id))
    assert response.json()["name"] == "Caf\u00e9 Bole Road"
    again = await api.post(AREAS, json={"name": "CAF\u00c9 BOLE ROAD"}, headers=with_key(member.id))
    assert again.status_code == 409


async def test_a_deleted_areas_name_can_be_used_again(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    await _area(db_session, name="Ayat", deleted_at=START)
    response = await api.post(AREAS, json={"name": "Ayat"}, headers=with_key(member.id))
    assert response.status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"name": "\u0958" * 100},  # 100 characters that NFC makes 200
        {"name": "Ay\u0000at"},
        {"name": "Ay\nat"},
        {"name": 7},
        {"name": None},
        {"name": "Ayat", "surprise": 1},
        [],
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member = await add_staff(db_session)
    response = await api.post(AREAS, json=body, headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# List and read


async def _areas_over_time(session: AsyncSession, *names: str) -> list[Area]:
    async with session.begin():
        return [
            await AreaFactory().create(session, name=name, created_at=START + timedelta(minutes=i))
            for i, name in enumerate(names)
        ]


async def test_staff_list_areas_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    areas = await _areas_over_time(db_session, "Ayat", "Bole", "CMC")
    await _area(db_session, name="Gone", deleted_at=START, created_at=START + timedelta(hours=1))
    response = await api.get(AREAS, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [item["name"] for item in response.json()["items"]] == ["CMC", "Bole", "Ayat"]
    assert response.json()["items"][0]["id"] == str(areas[-1].id)


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    await _areas_over_time(db_session, "A1", "A2", "A3", "A4", "A5")
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(AREAS, params=params, headers=signed_in(member.id))).json()
        seen += [item["name"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert (seen, cursor) == (["A5", "A4", "A3", "A2", "A1"], None)


@pytest.mark.parametrize(
    ("q", "found"),
    [
        ("ay", {"Ayat", "Ayat Condominium"}),
        ("AYAT C", {"Ayat Condominium"}),
        ("bole", {"Bole"}),
        ("at", set()),  # a prefix, not anywhere in the name
        ("50%", {"50% Corner"}),  # LIKE wildcards are plain characters
        ("5%", set()),
        ("5_", set()),
    ],
)
async def test_q_finds_names_starting_with_it(
    api: httpx.AsyncClient, db_session: AsyncSession, q: str, found: set[str]
) -> None:
    member = await add_staff(db_session)
    await _areas_over_time(db_session, "Ayat", "Ayat Condominium", "Bole", "50% Corner")
    await _area(db_session, name="Ayat Old", deleted_at=START)
    response = await api.get(AREAS, params={"q": q}, headers=signed_in(member.id))
    assert {item["name"] for item in response.json()["items"]} == found


@pytest.mark.parametrize("q", ["", "   "])
async def test_a_blank_q_is_no_filter(
    api: httpx.AsyncClient, db_session: AsyncSession, q: str
) -> None:
    """A picker may send its input box as it is."""
    member = await add_staff(db_session)
    await _areas_over_time(db_session, "Ayat", "Bole")
    response = await api.get(AREAS, params={"q": q}, headers=signed_in(member.id))
    assert {item["name"] for item in response.json()["items"]} == {"Ayat", "Bole"}


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        async with db_session.begin():
            await AreaFactory().create_batch(db_session, rows)
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(AREAS, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts == [2, 2]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"q": "x" * 101}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_list_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    member = await add_staff(db_session)
    response = await api.get(AREAS, params=params, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_staff_read_one_area(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    area = await _area(db_session, name="Ayat")
    response = await api.get(_member(area), headers=signed_in(member.id))
    assert (response.status_code, response.json()["name"]) == (200, "Ayat")


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_areas_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    admin_id = (await add_admin(db_session)).id  # a refused request expires loaded rows
    target = (await _area(db_session, deleted_at=START)).id if deleted else uuid.uuid4()
    for method in ("GET", "PATCH", "DELETE"):
        response = await api.request(
            method, _member(target), json={"name": "X"}, headers=signed_in(admin_id)
        )
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), method


async def test_a_malformed_area_id_is_422(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    assert (await api.get(f"{AREAS}/nope", headers=signed_in(member.id))).status_code == 422


# Rename


async def test_staff_rename_an_area(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    area_id = (await _area(db_session, name="Ayat")).id
    response = await api.patch(
        _member(area_id), json={"name": "Ayat (CMC side)"}, headers=signed_in(member.id)
    )
    assert (response.status_code, response.json()["name"]) == (200, "Ayat (CMC side)")
    assert response.headers["Cache-Control"] == "no-store"
    assert await _actions(db_session, area_id) == [
        ("area.renamed", {"name": "Ayat"}, {"name": "Ayat (CMC side)"})
    ]


async def test_renaming_to_the_same_name_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    area_id = (await _area(db_session, name="Ayat")).id
    response = await api.patch(
        _member(area_id), json={"name": " Ayat "}, headers=signed_in(member.id)
    )
    assert response.status_code == 200
    assert await _actions(db_session, area_id) == []


async def test_an_area_may_change_the_case_of_its_own_name(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    area_id = (await _area(db_session, name="ayat")).id
    response = await api.patch(
        _member(area_id), json={"name": "Ayat"}, headers=signed_in(member.id)
    )
    assert (response.status_code, response.json()["name"]) == (200, "Ayat")


async def test_renaming_to_a_name_in_use_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    await _area(db_session, name="Bole")
    area_id = (await _area(db_session, name="Ayat")).id
    response = await api.patch(
        _member(area_id), json={"name": "BOLE"}, headers=signed_in(member.id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "AREA_NAME_TAKEN")
    assert (await _reread(db_session, area_id)).name == "Ayat"


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": None}, {"name": "A", "x": 1}])
async def test_invalid_renames_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member = await add_staff(db_session)
    area_id = (await _area(db_session)).id
    response = await api.patch(_member(area_id), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# Delete


async def test_admins_delete_an_area(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    area_id = (await _area(db_session, name="Ayat")).id
    response = await api.delete(_member(area_id), headers=signed_in(admin.id))
    assert response.status_code == 204
    assert response.content == b""
    area = await _reread(db_session, area_id)
    assert area.deleted_at is not None  # kept: soft delete
    (entry,) = await audit_entries(db_session, area_id)
    assert (entry.action, entry.actor_staff_id) == ("area.deleted", admin.id)
    assert entry.before == {"deleted_at": None}
    assert entry.after is not None
    assert entry.after["deleted_at"] is not None
    listed = (await api.get(AREAS, headers=signed_in(admin.id))).json()["items"]
    assert str(area_id) not in {item["id"] for item in listed}


async def test_staff_cannot_delete(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    area_id = (await _area(db_session)).id
    response = await api.delete(_member(area_id), headers=signed_in(member.id))
    assert response.status_code == 403
    async with db_session.begin():
        assert (await db_session.get_one(Area, area_id)).deleted_at is None


# Who may call


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", AREAS, {"name": "Ayat"}),
        ("GET", AREAS, None),
        ("GET", f"{AREAS}/{uuid.UUID(int=1)}", None),
        ("PATCH", f"{AREAS}/{uuid.UUID(int=1)}", {"name": "Ayat"}),
        ("DELETE", f"{AREAS}/{uuid.UUID(int=1)}", None),
    ],
)
async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    method: str,
    path: str,
    body: object,
) -> None:
    gone = await add_admin(db_session, is_active=False)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert (await api.request(method, path, json=body, headers=key)).status_code == 401
    stranger = signed_in(uuid.uuid4())
    assert (
        await api.request(method, path, json=body, headers={**stranger, **key})
    ).status_code == 403
    deactivated = {**signed_in(gone.id), **key}
    assert (await api.request(method, path, json=body, headers=deactivated)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", AREAS),
        ("GET", AREAS),
        ("PATCH", f"{AREAS}/{uuid.UUID(int=1)}"),
        ("DELETE", f"{AREAS}/{uuid.UUID(int=1)}"),
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
    response = await api.request(
        method, path, json={"name": "Ayat"}, headers=with_key(uuid.uuid4())
    )
    assert response.status_code == 503


async def test_the_database_refuses_untrimmed_or_blank_names(db_session: AsyncSession) -> None:
    """The service trims; the table refuses anything that skipped it."""
    for name in (" Ayat", "", "   "):
        with pytest.raises(IntegrityError):
            async with db_session.begin():
                await AreaFactory().create(db_session, name=name)


def test_areas_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection, member = schema["paths"][AREAS], schema["paths"][f"{AREAS}/{{area_id}}"]
    assert {collection["post"]["operationId"], collection["get"]["operationId"]} == {
        "areas_create",
        "areas_list",
    }
    assert {op["operationId"] for op in member.values()} == {
        "areas_get",
        "areas_update",
        "areas_delete",
    }
    assert collection["post"]["responses"]["409"]["x-error-codes"] == [
        "AREA_NAME_TAKEN",
        "IDEMPOTENCY_IN_PROGRESS",
    ]
    assert member["patch"]["responses"]["409"]["x-error-codes"] == ["AREA_NAME_TAKEN"]
    assert member["delete"]["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]
    assert "204" in member["delete"]["responses"]
    names = {p["name"] for p in collection["get"]["parameters"]}
    assert names == {"limit", "cursor", "q", "sort"}
    assert schema["components"]["schemas"]["AreaWrite"]["additionalProperties"] is False


async def test_other_constraint_errors_are_not_reported_as_a_name_taken(
    db_session: AsyncSession,
) -> None:
    """Only the name index's violation becomes AREA_NAME_TAKEN; anything else stays an
    error (a bug, answered 500)."""
    async with db_session.begin():
        await AreaFactory().create(db_session, name="Ayat")

    async def flush_new(name: str) -> None:
        async with db_session.begin():
            db_session.add(Area(name=name))
            await service._flush(db_session)

    with pytest.raises(IntegrityError) as raised:
        await flush_new(" untrimmed")
    assert violated_unique_constraint(raised.value) is None  # a check, not a unique index
    with pytest.raises(AppError) as taken:
        await flush_new("AYAT")
    assert taken.value.code is ErrorCode.AREA_NAME_TAKEN
