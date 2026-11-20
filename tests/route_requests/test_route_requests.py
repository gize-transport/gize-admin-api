"""GZ-027: route requests, recorded by staff (from phone calls).

Unmet demand from individual riders: for two areas no route serves yet (`new_areas`, either
area may be one not listed yet, typed as text), or for a specific running route (`route`).
The rider's and contact's names, the phone, the school and the notes are personal data:
redacted in the audit log. One open request per phone, kind, route or area pair and rider;
siblings are separate requests. Only admins delete (soft).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode
from gize_api.main import GizeAPI, create_app
from gize_api.route_requests import service
from gize_api.route_requests.models import RouteRequest
from tests.factories import AreaFactory, RouteRequestFactory
from tests.route_requests.support import REQUESTS, add_request, request_path
from tests.routes.support import add_company, add_route
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory, every_page

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
CALLER = {
    "rider_type": "adult",
    "rider_name": "Abebe Kebede",
    "contact_name": "Abebe Kebede",
    "contact_phone": "0911 23 45 67",
}


async def _area(session: AsyncSession, name: str, **values: Any) -> uuid.UUID:  # noqa: ANN401
    async with session.begin():
        return (await AreaFactory().create(session, name=name, **values)).id


def _new_areas(**values: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"kind": "new_areas", **CALLER, **values}


# Create


async def test_staff_record_a_request_for_new_areas(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    ayat = await _area(db_session, "Ayat")
    body = _new_areas(
        from_area_id=str(ayat),
        to_area_name="  Lebu   Mebrat ",  # not listed yet
        preferred_morning_time="06:30",
        preferred_return_time="17:30",
        notes=" Works shifts on Fridays. ",
    )
    response = await api.post(REQUESTS, json=body, headers=with_key(member.id))
    assert response.status_code == 201
    created = response.json()
    assert created == {
        "id": created["id"],
        "kind": "new_areas",
        "from_area": {"id": str(ayat), "name": "Ayat"},
        "from_area_name": None,
        "to_area": None,
        "to_area_name": "Lebu Mebrat",
        "route": None,
        "rider_type": "adult",
        "rider_name": "Abebe Kebede",
        "school_name": None,
        "preferred_morning_time": "06:30",
        "preferred_return_time": "17:30",
        "contact_name": "Abebe Kebede",
        "contact_phone": "+251911234567",
        "notes": "Works shifts on Fridays.",
        "source": "staff",
        "status": "new",
        "matched_route": None,
        "decline_reason": None,
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }
    assert response.headers["Location"] == f"{REQUESTS}/{created['id']}"
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = await audit_entries(db_session, uuid.UUID(created["id"]))
    assert (entry.action, entry.actor_staff_id, entry.before) == (
        "route_request.created",
        member.id,
        None,
    )
    assert entry.after == {
        "kind": "new_areas",
        "from_area_id": str(ayat),
        "from_area_name": None,
        "to_area_id": None,
        "to_area_name": "Lebu Mebrat",
        "route_id": None,
        "rider_type": "adult",
        "rider_name": REDACTED,  # personal data
        "school_name": None,  # none: nothing to hide
        "preferred_morning_time": "06:30:00",
        "preferred_return_time": "17:30:00",
        "contact_name": REDACTED,
        "contact_phone": REDACTED,
        "notes": REDACTED,
        "source": "staff",
        "status": "new",
        "matched_route_id": None,
        "decline_reason": None,
        "deleted_at": None,
    }


@pytest.mark.parametrize("status", ["active", "paused"])
async def test_a_request_for_a_running_route(
    api: httpx.AsyncClient, db_session: AsyncSession, status: str
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, code="AYAT-01", status=status)
    body = {"kind": "route", "route_id": str(route), **CALLER}
    response = await api.post(REQUESTS, json=body, headers=with_key(member_id))
    assert response.status_code == 201
    created = response.json()
    assert created["route"] == {"id": str(route), "code": "AYAT-01"}
    assert (created["from_area"], created["to_area_name"]) == (None, None)


async def test_a_student_request_names_the_school(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, route_type="student", school_name="ICS", status="active")
    body = {
        "kind": "route",
        "route_id": str(route),
        **CALLER,
        "rider_type": "student",
        "rider_name": "Liya Abebe",
        "school_name": "ics",  # the route's school, ignoring case
    }
    response = await api.post(REQUESTS, json=body, headers=with_key(member_id))
    assert response.status_code == 201
    assert (response.json()["rider_type"], response.json()["school_name"]) == ("student", "ics")


async def test_a_retried_create_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    body = _new_areas(from_area_name="Ayat", to_area_name="Bole")
    headers = with_key(member_id)
    first = await api.post(REQUESTS, json=body, headers=headers)
    again = await api.post(REQUESTS, json=body, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert (again.json(), again.headers["Idempotent-Replayed"]) == (first.json(), "true")
    missing = await api.post(REQUESTS, json=body, headers=signed_in(member_id))
    assert missing.status_code == 428


async def test_one_open_request_per_rider_and_pair(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Same phone (however typed), kind, areas (names ignoring case) and rider: a 409.
    A sibling is another request; a closed request doesn't count."""
    member_id = (await add_staff(db_session)).id
    body = _new_areas(from_area_name="Ayat", to_area_name="Bole")
    first = await api.post(REQUESTS, json=body, headers=with_key(member_id))
    assert first.status_code == 201
    again = {
        **body,
        "contact_phone": "+251-911-234567",
        "rider_name": "abebe  KEBEDE",
        "from_area_name": "AYAT",
    }
    refused = await api.post(REQUESTS, json=again, headers=with_key(member_id))
    assert (refused.status_code, refused.json()["code"]) == (409, "ROUTE_REQUEST_EXISTS")
    sibling = {**body, "rider_name": "Liya Abebe"}
    assert (await api.post(REQUESTS, json=sibling, headers=with_key(member_id))).status_code == 201
    other_way = {**body, "from_area_name": "Bole", "to_area_name": "Ayat"}
    assert (
        await api.post(REQUESTS, json=other_way, headers=with_key(member_id))
    ).status_code == 201
    declined = {"status": "declined", "decline_reason": "No van planned there."}
    await api.put(
        f"{REQUESTS}/{first.json()['id']}/status", json=declined, headers=signed_in(member_id)
    )
    assert (await api.post(REQUESTS, json=body, headers=with_key(member_id))).status_code == 201


async def test_a_listed_area_typed_by_name_is_the_same_request(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A typed name is for an area not listed yet; a listed area's name names the area
    (owner decision, GZ-077: before, the two were different requests)."""
    member_id = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    by_name = _new_areas(from_area_name="Ayat", to_area_name="Bole")
    by_id = _new_areas(from_area_id=str(ayat), to_area_name="Bole")
    first = await api.post(REQUESTS, json=by_name, headers=with_key(member_id))
    assert (first.status_code, first.json()["from_area"]["id"]) == (201, str(ayat))
    again = await api.post(REQUESTS, json=by_id, headers=with_key(member_id))
    assert (again.status_code, again.json()["code"]) == (409, "ROUTE_REQUEST_EXISTS")


async def test_what_a_request_refers_to_must_exist_and_fit(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    gone_area = await _area(db_session, "Gone", deleted_at=START)
    company = await add_company(db_session)
    routes = {
        "forming": await add_route(db_session, status="forming"),
        "draft": await add_route(db_session),
        "deleted": await add_route(db_session, status="active", deleted_at=START),
        "corporate": await add_route(
            db_session,
            route_type="corporate",
            company_id=company,
            launch_minimum=None,
            status="active",
        ),
        "student": await add_route(
            db_session, route_type="student", school_name="ICS", status="active"
        ),
    }
    public = await add_route(db_session, status="active")
    cases: list[tuple[dict[str, Any], str]] = [
        (_new_areas(from_area_id=str(uuid.uuid4()), to_area_name="Bole"), "/from_area_id"),
        (_new_areas(from_area_name="Bole", to_area_id=str(gone_area)), "/to_area_id"),
        ({"kind": "route", "route_id": str(uuid.uuid4()), **CALLER}, "/route_id"),
        *(
            ({"kind": "route", "route_id": str(route), **CALLER}, "/route_id")
            for route in routes.values()  # not running, or not for adults
        ),
        (
            {
                "kind": "route",
                "route_id": str(public),
                **CALLER,
                "rider_type": "student",
                "school_name": "ICS",
            },
            "/route_id",
        ),
        (
            {
                "kind": "route",
                "route_id": str(routes["student"]),
                **CALLER,
                "rider_type": "student",
                "school_name": "Sandford",
            },
            "/route_id",
        ),
    ]
    for body, field in cases:
        response = await api.post(REQUESTS, json=body, headers=with_key(member_id))
        assert response.status_code == 422, body
        assert [e["field"] for e in response.json()["errors"]] == [field], body


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "route"},  # no route
        {"kind": "parcel"},
        {"to_area_name": None},  # no B
        {"from_area_id": str(uuid.UUID(int=1))},  # A by id and by name
        {"route_id": str(uuid.UUID(int=1))},  # a route on a new-areas request
        {"to_area_name": " ayat "},  # A and B the same
        {"school_name": "ICS"},  # an adult has no school
        {"rider_type": "student"},  # a student has one
        {"rider_type": "corporate"},  # company riders come through their company
        {"preferred_morning_time": "18:00", "preferred_return_time": "07:00"},
        {"preferred_morning_time": "6:30"},
        {"contact_phone": "0811234567"},
        {"rider_name": "x" * 101},
        {"rider_name": "   "},
        {"notes": "x" * 1001},
        {"source": "app"},  # set by the API
        {"status": "matched"},
        {"surprise": 1},
    ],
    ids=str,
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, Any]
) -> None:
    member_id = (await add_staff(db_session)).id
    body = {**_new_areas(from_area_name="Ayat", to_area_name="Bole"), **change}
    body = {key: value for key, value in body.items() if value is not None}
    response = await api.post(REQUESTS, json=body, headers=with_key(member_id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_the_same_listed_area_at_both_ends_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    body = _new_areas(from_area_id=str(ayat), to_area_id=str(ayat))
    response = await api.post(REQUESTS, json=body, headers=with_key(member_id))
    assert response.status_code == 422


# List and read


async def test_staff_list_requests_oldest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The order requests are served in: the oldest open request gets a free seat first."""
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, status="active")
    first = await add_request(db_session, created_at=datetime(2026, 11, 1, tzinfo=UTC))
    second = await add_request(
        db_session, kind="route", route_id=route, from_area_name=None, to_area_name=None,
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
    )  # fmt: skip
    declined = await add_request(
        db_session, status="declined", decline_reason="Too far.",
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
    )  # fmt: skip
    matched = await add_request(
        db_session, status="matched", matched_route_id=route,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
    )  # fmt: skip
    await add_request(db_session, deleted_at=START)

    async def ids(**params: str) -> list[uuid.UUID]:
        response = await api.get(REQUESTS, params=params, headers=signed_in(member_id))
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        return [uuid.UUID(item["id"]) for item in response.json()["items"]]

    assert await ids() == [first, second, declined, matched]
    assert await ids(open="true") == [first, second, matched]
    assert await ids(open="false") == [declined]
    assert await ids(status="matched") == [matched]
    assert await ids(kind="route") == [second]
    assert await ids(route_id=str(route)) == [second, matched]  # asked for or matched to
    page = await api.get(REQUESTS, params={"limit": 2}, headers=signed_in(member_id))
    rest = await api.get(
        REQUESTS,
        params={"limit": 2, "cursor": page.json()["next_cursor"]},
        headers=signed_in(member_id),
    )
    assert [item["id"] for item in rest.json()["items"]] == [str(declined), str(matched)]


async def test_staff_sort_requests_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """GZ-081: newest first on request; oldest first stays the default (the order they're
    served in). Requests made at the same moment always come in one order (by id)."""
    member_id = (await add_staff(db_session)).id
    at = [datetime(2026, 11, day, tzinfo=UTC) for day in (1, 2, 2, 2, 3)]
    made = [await add_request(db_session, created_at=moment) for moment in at]
    declined = await add_request(db_session, status="declined", decline_reason="Too far.",
                                 created_at=datetime(2026, 11, 4, tzinfo=UTC))  # fmt: skip
    oldest = [made[0], *sorted(made[1:4]), made[4], declined]
    headers = signed_in(member_id)
    expected = [str(i) for i in oldest]
    assert await every_page(api, REQUESTS, headers) == expected
    assert await every_page(api, REQUESTS, headers, sort="oldest") == expected
    assert await every_page(api, REQUESTS, headers, sort="newest") == expected[::-1]
    assert await every_page(api, REQUESTS, headers, sort="newest", open="true") == [
        str(i) for i in reversed(oldest[:-1])
    ]


@pytest.mark.parametrize("sorts", [("oldest", "newest"), ("newest", "oldest")], ids=str)
async def test_a_cursor_from_another_sort_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, sorts: tuple[str, str]
) -> None:
    member_id = (await add_staff(db_session)).id
    for day in (1, 2):
        await add_request(db_session, created_at=datetime(2026, 11, day, tzinfo=UTC))
    made, used = sorts
    first = await api.get(REQUESTS, params={"sort": made, "limit": "1"},
                          headers=signed_in(member_id))  # fmt: skip
    response = await api.get(REQUESTS, params={"sort": used, "cursor": first.json()["next_cursor"]},
                             headers=signed_in(member_id))  # fmt: skip
    assert (response.status_code, response.json()["code"]) == (422, "INVALID_CURSOR")


async def test_staff_list_the_requests_behind_a_demand_card(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """GZ-076 (frontend, GZW-041): a Demand card such as "Ayat → Piassa" opens exactly its
    requests. Each end is a listed area (`*_area_id`) or a typed one (`*_area_name`, the
    same name ignoring case, as the summary groups them)."""
    member_id = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    piassa = await _area(db_session, "Piassa")
    bole = await _area(db_session, "Bole")
    card = await add_request(
        db_session, from_area_id=ayat, from_area_name=None, to_area_id=piassa, to_area_name=None
    )
    reverse = await add_request(
        db_session, from_area_id=piassa, from_area_name=None, to_area_id=ayat, to_area_name=None
    )
    elsewhere = await add_request(
        db_session, from_area_id=ayat, from_area_name=None, to_area_id=bole, to_area_name=None
    )
    typed = await add_request(
        db_session, from_area_name="Tulu Dimtu", to_area_id=piassa, to_area_name=None
    )
    typed_both = await add_request(db_session, from_area_name="Ayat", to_area_name="tulu dimtu")
    await add_request(
        db_session, from_area_id=ayat, from_area_name=None, to_area_id=piassa,
        to_area_name=None, deleted_at=START,
    )  # fmt: skip

    async def ids(**params: str) -> set[uuid.UUID]:
        response = await api.get(REQUESTS, params=params, headers=signed_in(member_id))
        assert response.status_code == 200
        return {uuid.UUID(item["id"]) for item in response.json()["items"]}

    assert await ids(from_area_id=str(ayat), to_area_id=str(piassa)) == {card}
    assert await ids(from_area_id=str(ayat)) == {card, elsewhere}
    assert await ids(to_area_id=str(ayat)) == {reverse}
    assert await ids(to_area_id=str(piassa)) == {card, typed}
    assert await ids(from_area_name="TULU DIMTU ", to_area_id=str(piassa)) == {typed}
    assert await ids(to_area_name="Tulu Dimtu") == {typed_both}
    # A typed name finds typed ends only: "Ayat" typed is not the listed Ayat.
    assert await ids(from_area_name="ayat") == {typed_both}
    assert await ids(from_area_id=str(ayat), kind="new_areas", status="matched") == set()


async def test_every_demand_card_opens_exactly_its_requests(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """GZ-076: each area pair the summary counts, opened through the filters with the
    card's own conditions (`kind=new_areas&status=new`), lists as many requests as it
    counts. Pins the filters to the summary's grouping, typed names and case included."""
    member_id = (await add_staff(db_session)).id
    ayat = await _area(db_session, "Ayat")
    piassa = await _area(db_session, "Piassa")
    route = await add_route(db_session, status="active")
    for _ in range(3):
        await add_request(
            db_session, from_area_id=ayat, from_area_name=None, to_area_id=piassa,
            to_area_name=None,
        )  # fmt: skip
    await add_request(
        db_session, from_area_id=piassa, from_area_name=None, to_area_id=ayat, to_area_name=None
    )
    for typed in ("Tulu Dimtu", "TULU DIMTU", "tulu dimtu"):
        await add_request(db_session, from_area_name=typed, to_area_id=piassa, to_area_name=None)
    await add_request(db_session, from_area_name="Ayat", to_area_name="Piassa")
    await add_request(  # not counted: matched, or asking for a route
        db_session, from_area_id=ayat, from_area_name=None, to_area_id=piassa,
        to_area_name=None, status="matched", matched_route_id=route,
    )  # fmt: skip
    await add_request(
        db_session, kind="route", route_id=route, from_area_name=None, to_area_name=None
    )
    summary = await api.get(f"{REQUESTS}/summary", headers=signed_in(member_id))
    cards = summary.json()["area_pairs"]
    assert sorted(card["open"] for card in cards) == [1, 1, 3, 3]
    for card in cards:
        params = {"kind": "new_areas", "status": "new"}
        for end in ("from", "to"):
            if card[f"{end}_area"] is not None:
                params[f"{end}_area_id"] = card[f"{end}_area"]["id"]
            else:
                params[f"{end}_area_name"] = card[f"{end}_area_name"]
        response = await api.get(REQUESTS, params=params, headers=signed_in(member_id))
        assert len(response.json()["items"]) == card["open"], card


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, status="active")
    area = await _area(db_session, "Ayat")
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        async with db_session.begin():
            await RouteRequestFactory().create_batch(
                db_session, rows, from_area_id=area, from_area_name=None,
                status="matched", matched_route_id=route,
            )  # fmt: skip
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(REQUESTS, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts[0] == counts[1]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"open": "maybe"}, "VALIDATION_FAILED"),
        ({"status": "waiting"}, "VALIDATION_FAILED"),
        ({"route_id": "AYAT-01"}, "VALIDATION_FAILED"),
        ({"from_area_id": "Ayat"}, "VALIDATION_FAILED"),
        ({"to_area_id": "1"}, "VALIDATION_FAILED"),
        ({"from_area_name": " "}, "VALIDATION_FAILED"),
        ({"to_area_name": "x" * 101}, "VALIDATION_FAILED"),
        ({"from_area_name": "Ayat\nBole"}, "VALIDATION_FAILED"),
        # One end is listed or typed, never both.
        ({"from_area_id": str(uuid.uuid4()), "from_area_name": "Ayat"}, "VALIDATION_FAILED"),
        ({"to_area_id": str(uuid.uuid4()), "to_area_name": "Ayat"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"sort": "name"}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
        ({"sort": "newest", "cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_list_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    member_id = (await add_staff(db_session)).id
    response = await api.get(REQUESTS, params=params, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_staff_read_one_request(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session, rider_name="Liya Abebe")
    response = await api.get(request_path(request), headers=signed_in(member_id))
    assert (response.status_code, response.json()["rider_name"]) == (200, "Liya Abebe")
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_requests_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    target = await add_request(db_session, deleted_at=START) if deleted else uuid.uuid4()
    for method, body in (("GET", None), ("PATCH", {"notes": "x"})):
        response = await api.request(
            method, request_path(target), json=body, headers=signed_in(member_id)
        )
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


# Edit


async def test_staff_correct_a_request(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    request = await add_request(db_session)
    change = {
        "contact_phone": "0711 22 33 44",
        "rider_name": "Abebe K.",
        "preferred_morning_time": "07:00",
        "notes": "Call after 6 p.m.",
    }
    response = await api.patch(request_path(request), json=change, headers=signed_in(member.id))
    assert response.status_code == 200
    body = response.json()
    assert (body["contact_phone"], body["rider_name"], body["preferred_morning_time"]) == (
        "+251711223344",
        "Abebe K.",
        "07:00",
    )
    (entry,) = await audit_entries(db_session, request)
    assert (entry.action, entry.actor_staff_id) == ("route_request.updated", member.id)
    assert (entry.before, entry.after) == (
        {"contact_phone": REDACTED, "rider_name": REDACTED, "preferred_morning_time": None,
         "notes": None},
        {"contact_phone": REDACTED, "rider_name": REDACTED, "preferred_morning_time": "07:00:00",
         "notes": REDACTED},
    )  # fmt: skip


async def test_an_edit_to_what_is_there_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session, rider_name="Abebe Kebede")
    same = {"rider_name": "Abebe Kebede"}
    response = await api.patch(request_path(request), json=same, headers=signed_in(member_id))
    assert response.status_code == 200
    assert await audit_entries(db_session, request) == []


async def test_an_edit_into_another_open_request_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    pair = {"from_area_name": "Ayat", "to_area_name": "Bole", "contact_phone": "+251911234567"}
    await add_request(db_session, **pair, rider_name="Abebe")
    sibling = await add_request(db_session, **pair, rider_name="Liya")
    response = await api.patch(
        request_path(sibling), json={"rider_name": "ABEBE"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_REQUEST_EXISTS")


async def test_a_students_school_is_corrected(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """On a route request it stays the route's school (ignoring case)."""
    member_id = (await add_staff(db_session)).id
    student = {"rider_type": "student", "school_name": "Sandford"}
    for_areas = await add_request(db_session, **student)
    route = await add_route(db_session, route_type="student", school_name="ICS", status="active")
    for_route = await add_request(
        db_session, kind="route", route_id=route, from_area_name=None, to_area_name=None,
        rider_type="student", school_name="ICS",
    )  # fmt: skip
    for request, school, status in (
        (for_areas, "ICS", 200),
        (for_route, "ics", 200),
        (for_route, "Sandford", 422),
    ):
        response = await api.patch(
            request_path(request), json={"school_name": school}, headers=signed_in(member_id)
        )
        assert response.status_code == status, school


async def test_a_matched_students_school_stays_the_routes(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, route_type="student", school_name="ICS")
    request = await add_request(
        db_session, rider_type="student", school_name="ICS", status="matched",
        matched_route_id=route,
    )  # fmt: skip
    response = await api.patch(
        request_path(request), json={"school_name": "Sandford"}, headers=signed_in(member_id)
    )
    assert [e["field"] for e in response.json()["errors"]] == ["/school_name"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"preferred_morning_time": "18:00"}, "/preferred_morning_time"),
        ({"preferred_return_time": "06:00"}, "/preferred_return_time"),
        (
            {"preferred_morning_time": "18:00", "preferred_return_time": "17:00"},
            "/preferred_return_time",
        ),
    ],
    ids=str,
)
async def test_preferred_times_out_of_order_name_the_field(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, str], field: str
) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(
        db_session, preferred_morning_time=time(6, 30), preferred_return_time=time(17, 30)
    )
    response = await api.patch(request_path(request), json=change, headers=signed_in(member_id))
    assert [e["field"] for e in response.json()["errors"]] == [field]


async def test_a_closed_request_may_look_like_an_open_one(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Only open requests are kept apart: a correction to a closed one is never a 409."""
    member_id = (await add_staff(db_session)).id
    pair = {"from_area_name": "Ayat", "to_area_name": "Bole", "contact_phone": "+251911234567"}
    await add_request(db_session, **pair, rider_name="Abebe")
    closed = await add_request(
        db_session, **pair, rider_name="Liya", status="declined", decline_reason="Too far."
    )
    response = await api.patch(
        request_path(closed), json={"rider_name": "Abebe"}, headers=signed_in(member_id)
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"rider_name": None},
        {"school_name": "ICS"},  # an adult's request
        {"kind": "route"},  # what was asked for doesn't change: decline and record anew
        {"from_area_name": "Bole"},
        {"preferred_morning_time": "18:00"},  # after the stored return time
        {"contact_phone": "12345"},
        {"status": "fulfilled"},  # its own endpoint
        {"surprise": 1},
    ],
    ids=str,
)
async def test_invalid_edits_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, Any]
) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session, preferred_return_time=time(17, 30))
    response = await api.patch(request_path(request), json=change, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert await audit_entries(db_session, request) == []


# Delete


async def test_admins_delete_a_request(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin_id = (await add_admin(db_session)).id
    request = await add_request(db_session)
    response = await api.delete(request_path(request), headers=signed_in(admin_id))
    assert response.status_code == 204
    assert (await api.get(request_path(request), headers=signed_in(admin_id))).status_code == 404
    (entry,) = await audit_entries(db_session, request)
    assert (entry.action, entry.actor_staff_id, entry.before) == (
        "route_request.deleted",
        admin_id,
        {"deleted_at": None},
    )
    again = await api.delete(request_path(request), headers=signed_in(admin_id))
    assert (again.status_code, again.json()["code"]) == (404, "NOT_FOUND")


async def test_staff_cannot_delete(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session)
    response = await api.delete(request_path(request), headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert (await api.get(request_path(request), headers=signed_in(member_id))).status_code == 200


# Everyone else


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    request = await add_request(db_session)
    calls = [
        ("POST", REQUESTS, _new_areas(from_area_name="Ayat", to_area_name="Bole")),
        ("GET", REQUESTS, None),
        ("GET", f"{REQUESTS}/summary", None),
        ("GET", request_path(request), None),
        ("PATCH", request_path(request), {"notes": "x"}),
        ("PUT", f"{request_path(request)}/status", {"status": "fulfilled"}),
        ("DELETE", request_path(request), None),
    ]
    for method, path, body in calls:
        assert (await api.request(method, path, json=body)).status_code == 401, (method, path)
        for caller in (uuid.uuid4(), gone_id):
            response = await api.request(method, path, json=body, headers=with_key(caller))
            assert response.status_code == 403, (method, path)
    assert await audit_entries(db_session, request) == []


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(REQUESTS, headers=signed_in(uuid.uuid4()))
    assert response.status_code == 503


async def test_other_constraint_errors_are_not_reported_as_a_duplicate(
    db_session: AsyncSession,
) -> None:
    async with db_session.begin():
        await RouteRequestFactory().create(
            db_session, from_area_name="Ayat", to_area_name="Bole", contact_phone="+251911234567",
            rider_name="Abebe",
        )  # fmt: skip

    async def flush_new(**values: Any) -> None:  # noqa: ANN401
        async with db_session.begin():
            defaults: dict[str, Any] = {
                "kind": "new_areas", "from_area_name": "Ayat", "to_area_name": "Bole",
                "rider_type": "adult", "rider_name": "Abebe", "contact_name": "Abebe",
                "contact_phone": "+251911234567",
            }  # fmt: skip
            row = RouteRequest(**{**defaults, **values})
            db_session.add(row)
            await service._flush(db_session)

    with pytest.raises(IntegrityError):
        await flush_new(
            rider_name="Kebede", school_name="ICS"
        )  # a table check: adult with a school
    with pytest.raises(AppError) as exists:
        await flush_new()
    assert exists.value.code is ErrorCode.ROUTE_REQUEST_EXISTS


def test_route_requests_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection = schema["paths"][REQUESTS]
    member = schema["paths"][f"{REQUESTS}/{{request_id}}"]
    status = schema["paths"][f"{REQUESTS}/{{request_id}}/status"]
    summary = schema["paths"][f"{REQUESTS}/summary"]
    assert {op["operationId"] for op in collection.values()} == {
        "route_requests_create",
        "route_requests_list",
    }
    assert {op["operationId"] for op in member.values()} == {
        "route_requests_delete",
        "route_requests_get",
        "route_requests_update",
    }
    assert status["put"]["operationId"] == "route_requests_set_status"
    assert summary["get"]["operationId"] == "route_requests_summary"
    assert collection["post"]["responses"]["409"]["x-error-codes"] == [
        "IDEMPOTENCY_IN_PROGRESS",
        "ROUTE_REQUEST_EXISTS",
    ]
    assert status["put"]["responses"]["409"]["x-error-codes"] == ["INVALID_STATUS_CHANGE"]
    body = schema["components"]["schemas"]["RouteRequestCreate"]
    assert body["additionalProperties"] is False
    assert body["properties"]["notes"]["anyOf"][0]["maxLength"] == 1000
