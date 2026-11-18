"""GZ-077: requests for an area typed before it was listed name it once it is.

Asked by the frontend team (Demand, GZW-041): a request for "Tulu Dimtu", typed while it
wasn't an area, stayed "not listed" after staff added it, so its card couldn't start a
corridor or route. Owner decisions (2026-11-18):

- adding an area, or renaming one, links the open requests (`new`, `matched`) whose typed
  end has its name, ignoring case, in the same transaction, one audit entry each
  (`route_request.area_linked`); closed requests keep the typed text;
- a new request typed with a listed area's name names that area;
- both take one lock on area names first, so a request typed while its area is added is
  linked either way.

A link that would break a rule is skipped (the request keeps its text): the same rider's
same request already naming the area, or the area at both ends (both only by a rename).
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas.repository import AREA_NAMES_LOCK_ID
from gize_api.route_requests import repository
from tests.factories import AreaFactory
from tests.route_requests.support import REQUESTS, add_request, request_path
from tests.routes.support import add_route
from tests.staff.support import add_staff, audit_entries, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

AREAS = "/v1/areas"
START = datetime(2026, 11, 18, 6, 30, tzinfo=UTC)
CALLER = {
    "kind": "new_areas",
    "rider_type": "adult",
    "rider_name": "Abebe Kebede",
    "contact_name": "Abebe Kebede",
    "contact_phone": "0911 23 45 67",
}


async def _area(session: AsyncSession, name: str) -> uuid.UUID:
    async with session.begin():
        return (await AreaFactory().create(session, name=name)).id


async def _ends(api: httpx.AsyncClient, member: uuid.UUID, request: uuid.UUID) -> dict[str, Any]:
    body = (await api.get(request_path(request), headers=signed_in(member))).json()
    return {key: body[key] for key in ("from_area", "from_area_name", "to_area", "to_area_name")}


def _listed(area: uuid.UUID, name: str) -> dict[str, str]:
    return {"id": str(area), "name": name}


async def test_adding_an_area_links_the_open_requests_typed_with_its_name(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    piassa = await _area(db_session, "Piassa")
    route = await add_route(db_session, status="active")
    from_end = await add_request(
        db_session, from_area_name="Tulu Dimtu", to_area_id=piassa, to_area_name=None
    )
    to_end = await add_request(db_session, from_area_name="Lebu", to_area_name="TULU DIMTU")
    matched = await add_request(
        db_session, from_area_name="tulu dimtu", to_area_name="Ayat", status="matched",
        matched_route_id=route,
    )  # fmt: skip
    declined = await add_request(
        db_session, from_area_name="Tulu Dimtu", to_area_name="Ayat", status="declined",
        decline_reason="Too far.",
    )  # fmt: skip
    deleted = await add_request(
        db_session, from_area_name="Tulu Dimtu", to_area_name="Bole",
        deleted_at=START,
    )  # fmt: skip
    other = await add_request(db_session, from_area_name="Tulu", to_area_name="Dimtu")

    response = await api.post(AREAS, json={"name": "Tulu Dimtu"}, headers=with_key(member))
    assert response.status_code == 201
    tulu = uuid.UUID(response.json()["id"])
    assert response.json() == {**response.json(), "name": "Tulu Dimtu"}  # unchanged reply

    named = _listed(tulu, "Tulu Dimtu")
    assert await _ends(api, member, from_end) == {
        "from_area": named, "from_area_name": None,
        "to_area": _listed(piassa, "Piassa"), "to_area_name": None,
    }  # fmt: skip
    assert await _ends(api, member, to_end) == {
        "from_area": None, "from_area_name": "Lebu", "to_area": named, "to_area_name": None,
    }  # fmt: skip
    assert (await _ends(api, member, matched))["from_area"] == named
    assert (await _ends(api, member, declined))["from_area_name"] == "Tulu Dimtu"  # history
    assert await _ends(api, member, other) == {
        "from_area": None, "from_area_name": "Tulu", "to_area": None, "to_area_name": "Dimtu",
    }  # fmt: skip
    (entry,) = await audit_entries(db_session, from_end)  # factory rows have no "created"
    assert (entry.action, entry.actor_staff_id) == ("route_request.area_linked", member)
    assert (entry.before or {})["from_area_name"] == "Tulu Dimtu"
    assert (entry.before or {})["from_area_id"] is None
    assert ((entry.after or {})["from_area_id"], (entry.after or {})["from_area_name"]) == (
        str(tulu), None,
    )  # fmt: skip
    assert [e.action for e in await audit_entries(db_session, declined)] == []
    assert [e.action for e in await audit_entries(db_session, deleted)] == []

    summary = (await api.get(f"{REQUESTS}/summary", headers=signed_in(member))).json()
    assert {
        "from_area": named, "from_area_name": None,
        "to_area": _listed(piassa, "Piassa"), "to_area_name": None, "open": 1,
    } in summary["area_pairs"]  # fmt: skip


async def test_renaming_an_area_links_the_requests_typed_with_its_new_name(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    area = await _area(db_session, "Tulu")
    typed = await add_request(db_session, from_area_name="Tulu Dimtu", to_area_name="Piassa")
    response = await api.patch(
        f"{AREAS}/{area}", json={"name": "Tulu Dimtu"}, headers=signed_in(member)
    )
    assert response.status_code == 200
    assert (await _ends(api, member, typed))["from_area"] == _listed(area, "Tulu Dimtu")


async def test_renaming_an_area_to_its_own_name_links_requests_typed_before(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Requests typed with a listed area's name before GZ-077 stay typed; a rename to the
    same name (which changes nothing else) links them."""
    member = (await add_staff(db_session)).id
    area = await _area(db_session, "Ayat")
    typed = await add_request(db_session, from_area_name="AYAT", to_area_name="Piassa")
    response = await api.patch(f"{AREAS}/{area}", json={"name": "Ayat"}, headers=signed_in(member))
    assert response.status_code == 200
    assert (await _ends(api, member, typed))["from_area"] == _listed(area, "Ayat")
    assert [e.action for e in await audit_entries(db_session, area)] == []  # area unchanged


async def test_a_link_that_would_break_a_rule_is_skipped(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Renaming Bole to "Ayat": a typed "Ayat" → Bole request would go from Bole to Bole,
    and a typed "Ayat" → Piassa would repeat the same rider's open Bole → Piassa."""
    member = (await add_staff(db_session)).id
    bole = await _area(db_session, "Bole")
    piassa = await _area(db_session, "Piassa")
    both_ends = await add_request(
        db_session, from_area_name="Ayat", to_area_id=bole, to_area_name=None
    )
    listed = await add_request(
        db_session, from_area_id=bole, from_area_name=None, to_area_id=piassa,
        to_area_name=None, contact_phone="+251911000001", rider_name="Liya Abebe",
    )  # fmt: skip
    repeat = await add_request(
        db_session, from_area_name="Ayat", to_area_id=piassa, to_area_name=None,
        contact_phone="+251911000001", rider_name="Liya Abebe",
    )  # fmt: skip
    linked = await add_request(
        db_session, from_area_name="ayat", to_area_id=piassa, to_area_name=None,
        contact_phone="+251911000002",
    )  # fmt: skip
    response = await api.patch(f"{AREAS}/{bole}", json={"name": "Ayat"}, headers=signed_in(member))
    assert response.status_code == 200
    assert (await _ends(api, member, both_ends))["from_area_name"] == "Ayat"
    assert (await _ends(api, member, repeat))["from_area_name"] == "Ayat"
    assert (await _ends(api, member, listed))["from_area"] == _listed(bole, "Ayat")
    assert (await _ends(api, member, linked))["from_area"] == _listed(bole, "Ayat")
    assert [e.action for e in await audit_entries(db_session, repeat)] == []


async def test_a_request_typed_with_a_listed_name_names_the_area(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    await _area_deleted(db_session, "Lebu")
    body = {**CALLER, "from_area_name": " ayat ", "to_area_name": "Lebu"}
    response = await api.post(REQUESTS, json=body, headers=with_key(member))
    assert response.status_code == 201
    created = response.json()
    assert (created["from_area"], created["from_area_name"]) == (_listed(ayat, "Ayat"), None)
    assert (created["to_area"], created["to_area_name"]) == (None, "Lebu")  # deleted: typed
    (entry,) = await audit_entries(db_session, uuid.UUID(created["id"]))
    assert (entry.after or {})["from_area_id"] == str(ayat)

    again = await api.post(
        REQUESTS, json={**body, "from_area_id": str(ayat), "from_area_name": None},
        headers=with_key(member),
    )  # fmt: skip
    assert (again.status_code, again.json()["code"]) == (409, "ROUTE_REQUEST_EXISTS")

    piassa = await _area(db_session, "Piassa")
    for ends in (
        {"from_area_id": str(piassa), "to_area_name": "AYAT"},
        {"from_area_name": "AYAT", "to_area_id": str(piassa)},
    ):
        both = await api.post(REQUESTS, json={**CALLER, **ends}, headers=with_key(member))
        assert both.status_code == 201
        assert {both.json()["from_area"]["id"], both.json()["to_area"]["id"]} == {
            str(piassa), str(ayat),
        }  # fmt: skip


async def test_a_typed_name_of_the_area_at_the_other_end_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    for body, field in (
        ({"from_area_name": "AYAT", "to_area_id": str(ayat)}, "from_area_name"),
        ({"from_area_id": str(ayat), "to_area_name": "ayat"}, "to_area_name"),
    ):
        response = await api.post(REQUESTS, json={**CALLER, **body}, headers=with_key(member))
        assert response.status_code == 422
        assert [error["field"] for error in response.json()["errors"]] == [f"/{field}"]


@pytest.mark.parametrize("first", ["request", "area"])
async def test_a_request_typed_while_its_area_is_added_is_linked(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    first: str,
) -> None:
    """Both wait on the area-names lock, granted in turn: typed first, the add links the
    request (audited); added first, the request finds the area."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
    body = {**CALLER, "from_area_name": "Tulu Dimtu", "to_area_name": "Piassa"}
    calls = {
        "area": lambda: committed_api.post(
            AREAS, json={"name": "Tulu Dimtu"}, headers=with_key(member)
        ),
        "request": lambda: committed_api.post(REQUESTS, json=body, headers=with_key(member)),
    }
    second = "area" if first == "request" else "request"
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = conn.transaction()
        await held.start()
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AREA_NAMES_LOCK_ID)
        tasks = {first: asyncio.create_task(calls[first]())}
        await wait_for_lock_waiters(database, count=1)
        tasks[second] = asyncio.create_task(calls[second]())
        await wait_for_lock_waiters(database, count=2)
        await held.rollback()
        added, recorded = await tasks["area"], await tasks["request"]
    finally:
        await conn.close()
    assert (added.status_code, recorded.status_code) == (201, 201)
    request = uuid.UUID(recorded.json()["id"])
    ends = await _ends(committed_api, member, request)
    assert ends["from_area"] == _listed(uuid.UUID(added.json()["id"]), "Tulu Dimtu")
    async with committed_sessionmaker() as session:
        actions = [entry.action for entry in await audit_entries(session, request)]
    linked = ["route_request.area_linked"] if first == "request" else []
    assert actions == ["route_request.created", *linked]


async def test_a_rename_waiting_on_a_request_lets_inserts_refer_to_the_area(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """From review: accepting a seat offer holds its request, then inserts a rider whose
    areas are checked as foreign keys (FOR KEY SHARE on the area). A rename holding the
    area and waiting on that request would close a circle, unless it locks the area FOR NO
    KEY UPDATE, which a foreign key check doesn't wait for."""
    async with committed_sessionmaker() as session:
        member = (await add_staff(session)).id
        bole = await _area(session, "Bole")
        typed = await add_request(session, from_area_name="Ayat", to_area_name="Piassa")
    holder = await asyncpg.connect(database.migrator, timeout=3)
    checker = await asyncpg.connect(database.migrator, timeout=3)
    try:
        held = holder.transaction()
        await held.start()
        await holder.execute("SELECT 1 FROM gize.route_requests WHERE id = $1 FOR UPDATE", typed)
        rename = asyncio.create_task(
            committed_api.patch(f"{AREAS}/{bole}", json={"name": "Ayat"}, headers=signed_in(member))
        )
        await wait_for_lock_waiters(database, count=1)  # the rename holds Bole, waits
        async with checker.transaction():
            await checker.execute("SET LOCAL lock_timeout = '2s'")
            found = await checker.fetchval(
                "SELECT 1 FROM gize.areas WHERE id = $1 FOR KEY SHARE", bole
            )
        assert found == 1
        await held.rollback()
        response = await rename
    finally:
        await holder.close()
        await checker.close()
    assert response.status_code == 200
    ends = await _ends(committed_api, member, typed)
    assert ends["from_area"] == _listed(bole, "Ayat")


async def test_a_link_an_edit_turned_into_a_repeat_meanwhile_is_skipped(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From review: a request edit (PATCH, phone or name) committed between the check and
    the link makes it repeat the same rider's open request. The unique index catches it in
    a savepoint: that link is skipped, the others and the rename go through."""
    member = (await add_staff(db_session)).id
    bole = await _area(db_session, "Bole")
    piassa = await _area(db_session, "Piassa")
    await add_request(
        db_session, from_area_id=bole, from_area_name=None, to_area_id=piassa,
        to_area_name=None, contact_phone="+251911000001", rider_name="Liya Abebe",
    )  # fmt: skip
    repeat = await add_request(
        db_session, from_area_name="Ayat", to_area_id=piassa, to_area_name=None,
        contact_phone="+251911000001", rider_name="Liya Abebe",
    )  # fmt: skip
    linked = await add_request(db_session, from_area_name="AYAT", to_area_name="Lebu")

    async def checked_before_the_edit(*_: object, **__: object) -> bool:
        return False

    monkeypatch.setattr(repository, "open_request_exists", checked_before_the_edit)
    response = await api.patch(f"{AREAS}/{bole}", json={"name": "Ayat"}, headers=signed_in(member))
    assert response.status_code == 200
    assert (await _ends(api, member, repeat))["from_area_name"] == "Ayat"
    assert (await _ends(api, member, linked))["from_area"] == _listed(bole, "Ayat")
    assert [e.action for e in await audit_entries(db_session, repeat)] == []


async def test_a_refused_rename_links_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    await _area(db_session, "Ayat")
    bole = await _area(db_session, "Bole")
    typed = await add_request(db_session, from_area_name="Ayat", to_area_name="Piassa")
    response = await api.patch(f"{AREAS}/{bole}", json={"name": "AYAT"}, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (409, "AREA_NAME_TAKEN")
    taken = await api.post(AREAS, json={"name": "ayat"}, headers=with_key(member))
    assert (taken.status_code, taken.json()["code"]) == (409, "AREA_NAME_TAKEN")
    assert (await _ends(api, member, typed))["from_area_name"] == "Ayat"


async def _area_deleted(session: AsyncSession, name: str) -> None:
    async with session.begin():
        await AreaFactory().create(session, name=name, deleted_at=START)
