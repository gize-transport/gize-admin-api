"""GZ-075: a route's offer queue, in the order "Offer a free seat" picks ("today" is 15
November 2026).

`GET /v1/routes/{id}/offer-queue` lists the open requests waiting for the route (asked for
it, or matched to it). First those a seat can be offered to, numbered: oldest first, but
anyone offered a seat on this route before (expired or withdrawn) after the others. Then
those it can't, oldest first, with the reason: an open offer (on any route), or a student
(seats are offered to adults). The offer and the queue share the same rules, so position
1 is always whom it picks.
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.core.db import get_session
from gize_api.core.pagination import Cursor, encode_cursor
from gize_api.main import GizeAPI
from gize_api.offers.models import SeatOffer
from gize_api.route_requests.models import RequestKind, RequestStatus, RiderType
from gize_api.routes.models import RouteStatus
from tests.factories import RouteRequestFactory
from tests.offers.test_offers import _offer, _running
from tests.registrations.support import Trip, add_route, add_trip
from tests.staff.support import add_admin, add_staff, signed_in, with_key


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


def _cursor(raw: list[object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(raw).encode()).rstrip(b"=").decode()


def _queue_path(route: uuid.UUID) -> str:
    return f"/v1/routes/{route}/offer-queue"


async def _ask(
    session: AsyncSession,
    route: uuid.UUID,
    name: str,
    *,
    day: int,
    **values: Any,  # noqa: ANN401
) -> uuid.UUID:
    async with session.begin():
        request = await RouteRequestFactory().create(
            session,
            **{
                "kind": RequestKind.ROUTE,
                "route_id": route,
                "from_area_name": None,
                "to_area_name": None,
                "rider_name": name,
                "contact_phone": f"+2519110001{day:02d}",
                "created_at": datetime(2026, 10, day, tzinfo=UTC),
                **values,
            },
        )
        return request.id


async def _queue(
    api: httpx.AsyncClient, staff: uuid.UUID, route: uuid.UUID, **params: str | int
) -> dict[str, Any]:
    response = await api.get(_queue_path(route), params=params, headers=signed_in(staff))
    assert response.status_code == 200, response.json()
    assert response.headers["Cache-Control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def _rows(page: dict[str, Any]) -> list[tuple[str, int | None, bool, str | None]]:
    return [
        (item["request"]["rider_name"], item["position"], item["offered_before"],
         item["not_offerable"])
        for item in page["items"]
    ]  # fmt: skip


async def _b07(
    api: httpx.AsyncClient, session: AsyncSession, trip: Trip, staff: uuid.UUID
) -> uuid.UUID:
    """The frontend team's example (2026-11-12): Almaz offered a seat here before, Dawit with
    an open offer on another route, Meron asking last; with a student and a request matched
    to the route besides."""
    route = await _running(session, trip, free=3, code="B-07")
    elsewhere = await _running(session, trip, free=1, code="B-09")
    almaz = await _ask(session, route, "Almaz", day=1)
    offered = await _offer(api, staff, route)  # to Almaz, the only one asking yet
    assert offered.json()["request_id"] == str(almaz)
    withdrawn = await api.post(
        f"/v1/offers/{offered.json()['id']}/withdraw", json={}, headers=with_key(staff)
    )
    assert withdrawn.status_code == 200, withdrawn.json()
    await _ask(session, route, "Selam", day=2, rider_type=RiderType.STUDENT,
               school_name="Bole Primary")  # fmt: skip
    dawit = await _ask(session, route, "Dawit", day=5, status=RequestStatus.MATCHED,
                       matched_route_id=elsewhere)  # fmt: skip
    assert (await _offer(api, staff, elsewhere)).json()["request_id"] == str(dawit)
    await _ask(session, route, "Meron", day=10)
    async with session.begin():
        await RouteRequestFactory().create(
            session, status=RequestStatus.MATCHED, matched_route_id=route, rider_name="Tsehay",
            created_at=datetime(2026, 10, 12, tzinfo=UTC),
        )  # fmt: skip
    return route


async def test_the_queue_shows_the_order_the_offer_picks(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _b07(api, db_session, trip, member)
    page = await _queue(api, member, route)
    assert _rows(page) == [
        ("Meron", 1, False, None),
        ("Tsehay", 2, False, None),  # matched to the route: waits for it too
        ("Almaz", 3, True, None),  # offered a seat here before: after the others
        ("Selam", None, False, "student"),  # seats are offered to adults
        ("Dawit", None, False, "open_offer"),  # on B-09
    ]
    assert page["next_cursor"] is None
    # The request is the request list's own shape.
    (meron,) = [i["request"] for i in page["items"] if i["request"]["rider_name"] == "Meron"]
    listed = await api.get(f"/v1/route-requests/{meron['id']}", headers=signed_in(member))
    assert listed.json() == meron
    offered = await _offer(api, member, route)
    assert offered.json()["request_id"] == meron["id"]
    assert _rows(await _queue(api, member, route))[:3] == [
        ("Tsehay", 1, False, None),
        ("Almaz", 2, True, None),
        ("Selam", None, False, "student"),
    ]


async def test_position_1_is_always_whom_the_offer_picks(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Offer seat after seat: each goes to the queue's head, until nobody can be offered."""
    route = await _b07(api, db_session, trip, member)
    for _ in range(3):
        (head,) = [i for i in (await _queue(api, member, route))["items"] if i["position"] == 1]
        offered = await _offer(api, member, route)
        assert offered.status_code == 201, offered.json()
        assert offered.json()["request_id"] == head["request"]["id"]
    page = await _queue(api, member, route)
    assert [item["position"] for item in page["items"]] == [None] * 5
    assert (await _offer(api, member, route)).status_code == 409  # the route is full now


async def test_pages_continue_the_numbering(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _b07(api, db_session, trip, member)
    seen: list[tuple[str, int | None, bool, str | None]] = []
    params: dict[str, str | int] = {"limit": 2}
    pages = 0
    while True:
        page = await _queue(api, member, route, **params)
        pages += 1
        seen += _rows(page)
        if page["next_cursor"] is None:
            break
        params = {"limit": 2, "cursor": page["next_cursor"]}
    assert pages == 3
    assert [(name, position) for name, position, *_ in seen] == [
        ("Meron", 1), ("Tsehay", 2), ("Almaz", 3), ("Selam", None), ("Dawit", None),
    ]  # fmt: skip


async def test_offers_past_their_48_hours_count_as_lapsed(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Still `open` in the table until the route is next offered, but lapsed: here the
    request goes after the others, as the offer would put it; elsewhere it's free again."""
    route = await _running(db_session, trip, free=2, code="B-07")
    elsewhere = await _running(db_session, trip, free=1, code="B-09")
    almaz = await _ask(db_session, route, "Almaz", day=1)
    assert (await _offer(api, member, route)).json()["request_id"] == str(almaz)
    dawit = await _ask(db_session, route, "Dawit", day=5, status=RequestStatus.MATCHED,
                       matched_route_id=elsewhere)  # fmt: skip
    assert (await _offer(api, member, elsewhere)).json()["request_id"] == str(dawit)
    await _ask(db_session, route, "Meron", day=10)
    async with db_session.begin():
        await db_session.execute(
            update(SeatOffer).values(expires_at=datetime(2026, 11, 1, tzinfo=UTC))
        )
    assert _rows(await _queue(api, member, route)) == [
        ("Dawit", 1, False, None),
        ("Meron", 2, False, None),
        ("Almaz", 3, True, None),
    ]
    assert (await _offer(api, member, route)).json()["request_id"] == str(dawit)


async def test_requests_asked_at_the_same_moment_page_by_id(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    ids = sorted([
        await _ask(db_session, route, "Abel", day=3, contact_phone="+251911000201"),
        await _ask(db_session, route, "Bethel", day=3, contact_phone="+251911000202"),
    ])  # fmt: skip
    first = await _queue(api, member, route, limit=1)
    second = await _queue(api, member, route, limit=1, cursor=first["next_cursor"])
    assert [(i["request"]["id"], i["position"]) for i in first["items"] + second["items"]] == [
        (str(ids[0]), 1), (str(ids[1]), 2),
    ]  # fmt: skip
    assert second["next_cursor"] is None


async def test_closed_and_deleted_requests_are_not_in_the_queue(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    await _ask(db_session, route, "Fulfilled", day=1, status=RequestStatus.FULFILLED)
    await _ask(db_session, route, "Declined", day=2, status=RequestStatus.DECLINED,
               decline_reason="Moved away.")  # fmt: skip
    await _ask(db_session, route, "Deleted", day=3, deleted_at=datetime(2026, 11, 2, tzinfo=UTC))
    await _ask(db_session, route, "Waiting", day=4)
    assert _rows(await _queue(api, member, route)) == [("Waiting", 1, False, None)]


async def test_a_route_not_taking_offers_still_shows_its_queue(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """Only offering is refused on a draft or paused route; who's waiting is still useful."""
    route = await add_route(db_session, trip, code="B-08", status=RouteStatus.DRAFT)
    await _ask(db_session, route, "Hirut", day=3)
    assert _rows(await _queue(api, member, route)) == [("Hirut", 1, False, None)]
    assert (await _offer(api, member, route)).status_code == 409


async def test_an_empty_queue(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _running(db_session, trip)
    assert await _queue(api, member, route) == {"items": [], "next_cursor": None}


async def test_unknown_routes_are_404(api: httpx.AsyncClient, member: uuid.UUID) -> None:
    response = await api.get(_queue_path(uuid.uuid4()), headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        # The request list's cursor (time and id) is not this list's.
        {"cursor": encode_cursor(Cursor(datetime(2026, 10, 1, tzinfo=UTC), uuid.uuid4()))},
        {"cursor": _cursor([7, "2026-10-01T00:00:00+00:00", str(uuid.uuid4())])},  # no group 7
        {"cursor": _cursor([True, "2026-10-01T00:00:00+00:00", str(uuid.uuid4())])},
        {"cursor": _cursor([0, "2026-10-01T00:00:00", str(uuid.uuid4())])},  # no time zone
        {"cursor": _cursor([0, "2026-10-01T00:00:00+00:00", 5])},  # an id that isn't text
        {"limit": 101},
        {"limit": 0},
        {"route": "B-07"},
    ],
)
async def test_bad_parameters_are_refused(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
    params: dict[str, str | int],
) -> None:
    route = await _running(db_session, trip)
    response = await api.get(_queue_path(route), params=params, headers=signed_in(member))
    assert response.status_code == 422, response.json()
    if "cursor" in params:
        assert response.json()["code"] == "INVALID_CURSOR"


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip
) -> None:
    route = await _running(db_session, trip)
    gone = (await add_admin(db_session, is_active=False)).id
    assert (await api.get(_queue_path(route))).status_code == 401
    for caller in (uuid.uuid4(), gone):
        response = await api.get(_queue_path(route), headers=signed_in(caller))
        assert response.status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(_queue_path(uuid.uuid4()), headers=signed_in(uuid.uuid4()))
    assert response.status_code == 503


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    """No N+1 (engineering-standards.md): as many queries for 1 request as for 21."""
    route = await _running(db_session, trip)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for day, rows in ((1, 1), (2, 20)):
        for n in range(rows):
            await _ask(db_session, route, f"Asker {day}.{n}", day=day,
                       contact_phone=f"+25191120{day}{n:03d}")  # fmt: skip
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            await _queue(api, member, route)
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    # The caller, the route (and its served areas, as the route lookup loads them), the page.
    assert counts == [4, 4]
