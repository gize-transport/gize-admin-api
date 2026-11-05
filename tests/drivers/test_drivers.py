"""GZ-050: drivers (owner decision, 2026-11-05: basic information only for the MVP).

Staff add, list (search by name or plate), read and edit drivers: full name, phone (an
Ethiopian mobile, one live driver per phone), address, and the plate number of the van
they drive (capitals, spacing normalised, one live driver per plate). A driver is active
or inactive; admins delete them (soft). The phone and address are personal data: redacted
in the audit log, never in a URL. Documents, inspection and approval come later.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.drivers.models import Driver
from gize_api.main import GizeAPI
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key

DRIVERS = "/v1/drivers"
NEW = {
    "full_name": "Tesfaye Alemu",
    "phone": "0911 23 45 67",
    "address": "Bole, near Edna Mall",
    "plate": "aa  3-b12345",
}


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def _driver(session: AsyncSession, **values: object) -> uuid.UUID:
    async with session.begin():
        driver = Driver(
            **{
                "full_name": "Abebe Driver",
                "phone": f"+2519{uuid.uuid4().int % 10**8:08d}",
                "address": "Megenagna",
                "plate": f"AA 3-{uuid.uuid4().int % 10**5:05d}",
                **values,
            }
        )
        session.add(driver)
        await session.flush()
        return driver.id


def _path(driver: uuid.UUID | str) -> str:
    return f"{DRIVERS}/{driver}"


async def test_staff_add_a_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    response = await api.post(DRIVERS, json=NEW, headers=with_key(member))
    assert response.status_code == 201, response.json()
    created = response.json()
    assert created == {
        "id": created["id"],
        "full_name": "Tesfaye Alemu",
        "phone": "+251911234567",
        "address": "Bole, near Edna Mall",
        "plate": "AA 3-B12345",  # capitals, one space
        "is_active": True,
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }
    assert response.headers["Location"] == _path(created["id"])
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = await audit_entries(db_session, uuid.UUID(created["id"]))
    assert (entry.action, entry.actor_staff_id) == ("driver.created", member)
    assert entry.after == {
        "full_name": REDACTED, "phone": REDACTED, "address": REDACTED,
        "plate": "AA 3-B12345", "is_active": True, "deleted_at": None,
    }  # fmt: skip


async def test_a_retried_add_replays(api: httpx.AsyncClient, member: uuid.UUID) -> None:
    headers = with_key(member)
    first = await api.post(DRIVERS, json=NEW, headers=headers)
    again = await api.post(DRIVERS, json=NEW, headers=headers)
    assert (first.status_code, again.status_code, again.json()) == (201, 201, first.json())
    keyless = await api.post(DRIVERS, json=NEW, headers=signed_in(member))
    assert (keyless.status_code, keyless.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")


@pytest.mark.parametrize(
    ("taken", "code"),
    [({"phone": "+251911234567"}, "DRIVER_EXISTS"), ({"plate": "AA 3-B12345"}, "PLATE_TAKEN")],
)
async def test_a_phone_or_plate_belongs_to_one_live_driver(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    member: uuid.UUID,
    taken: dict[str, str],
    code: str,
) -> None:
    await _driver(db_session, **taken)
    response = await api.post(DRIVERS, json=NEW, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (409, code)


async def test_a_deleted_drivers_phone_and_plate_are_free(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    await _driver(db_session, phone="+251911234567", plate="AA 3-B12345",
                  deleted_at=datetime.now(UTC))  # fmt: skip
    assert (await api.post(DRIVERS, json=NEW, headers=with_key(member))).status_code == 201


@pytest.mark.parametrize(
    "changes",
    [
        {"full_name": "  "},
        {"full_name": "x" * 101},
        {"phone": "0811234567"},
        {"phone": None},
        {"address": "x" * 201},
        {"address": None},
        {"plate": "!"},
        {"plate": "A"},
        {"plate": "x" * 21},
        {"plate": None},
        {"is_owner": True},
    ],
    ids=str,
)
async def test_a_bad_driver_is_422(
    api: httpx.AsyncClient, member: uuid.UUID, changes: dict[str, object]
) -> None:
    body = {key: value for key, value in {**NEW, **changes}.items() if value is not None}
    response = await api.post(DRIVERS, json=body, headers=with_key(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_staff_read_one_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    gone = await _driver(db_session, deleted_at=datetime.now(UTC))
    assert (await api.get(_path(driver), headers=signed_in(member))).status_code == 200
    for missing in (gone, uuid.uuid4()):
        response = await api.get(_path(missing), headers=signed_in(member))
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_the_list_finds_by_name_or_plate_and_filters(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    tesfaye = await _driver(db_session, full_name="Tesfaye Alemu", plate="AA 3-B12345",
                            created_at=datetime(2026, 10, 1, tzinfo=UTC))  # fmt: skip
    almaz = await _driver(
        db_session, full_name="Almaz Kebede", plate="OR 2-77777", is_active=False,
        created_at=datetime(2026, 10, 2, tzinfo=UTC),
    )  # fmt: skip
    await _driver(db_session, deleted_at=datetime.now(UTC))

    async def ids(**params: str) -> list[str]:
        response = await api.get(DRIVERS, params=params, headers=signed_in(member))
        assert response.status_code == 200, response.json()
        return [item["id"] for item in response.json()["items"]]

    assert await ids() == [str(almaz), str(tesfaye)]  # newest first, deleted left out
    assert await ids(q="tes") == [str(tesfaye)]
    assert await ids(q="aa 3") == [str(tesfaye)]  # a plate, typed any case or spacing
    assert await ids(q="or  2-7") == [str(almaz)]
    assert await ids(is_active="false") == [str(almaz)]
    assert await ids(limit="1") == [str(almaz)]
    bad = await api.get(DRIVERS, params={"phone": "+251911234567"}, headers=signed_in(member))
    assert bad.status_code == 422  # phones never go in URLs


async def test_staff_edit_a_driver(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    driver = await _driver(db_session)
    change = {"address": "CMC", "plate": "aa 3-c99999", "is_active": False}
    response = await api.patch(_path(driver), json=change, headers=signed_in(member))
    assert response.status_code == 200, response.json()
    assert (response.json()["address"], response.json()["plate"], response.json()["is_active"]) == (
        "CMC", "AA 3-C99999", False,
    )  # fmt: skip
    (entry,) = await audit_entries(db_session, driver)
    assert entry.action == "driver.updated"
    assert entry.before == {"address": REDACTED, "plate": entry.before["plate"], "is_active": True}  # type: ignore[index]
    nothing = await api.patch(_path(driver), json={"address": "CMC"}, headers=signed_in(member))
    assert nothing.status_code == 200
    assert len(await audit_entries(db_session, driver)) == 1  # the same value records nothing


@pytest.mark.parametrize(
    ("taken", "code"),
    [({"phone": "+251922334455"}, "DRIVER_EXISTS"), ({"plate": "OR 2-77777"}, "PLATE_TAKEN")],
)
async def test_an_edit_to_a_taken_phone_or_plate_is_409(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    member: uuid.UUID,
    taken: dict[str, str],
    code: str,
) -> None:
    await _driver(db_session, **taken)
    driver = await _driver(db_session)
    response = await api.patch(_path(driver), json=taken, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (409, code)


@pytest.mark.parametrize(
    "change", [{}, {"phone": None}, {"plate": "!"}, {"is_active": "no"}], ids=str
)
async def test_a_bad_edit_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID, change: dict[str, object]
) -> None:
    driver = await _driver(db_session)
    response = await api.patch(_path(driver), json=change, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_admins_delete_drivers_and_staff_cannot(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    admin = (await add_admin(db_session)).id
    driver = await _driver(db_session)
    refused = await api.delete(_path(driver), headers=signed_in(member))
    assert (refused.status_code, refused.json()["code"]) == (403, "FORBIDDEN")
    assert (await api.delete(_path(driver), headers=signed_in(admin))).status_code == 204
    assert (await api.get(_path(driver), headers=signed_in(admin))).status_code == 404
    actions = [entry.action for entry in await audit_entries(db_session, driver)]
    assert actions == ["driver.deleted"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", DRIVERS), ("POST", DRIVERS), ("GET", _path(uuid.UUID(int=1))),
     ("PATCH", _path(uuid.UUID(int=1))), ("DELETE", _path(uuid.UUID(int=1)))],
)  # fmt: skip
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    unreachable_sessions: AsyncSession,
    method: str,
    path: str,
) -> None:
    admin = (await add_admin(db_session)).id

    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    body = NEW if method == "POST" else {"address": "CMC"}
    response = await api.request(method, path, json=body, headers=with_key(admin))
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
