"""GZ-027: a request's status, moved by staff.

`new` → `matched` (to a route: a forming one, or a van to come) → `fulfilled`; `new` →
`fulfilled` directly; `new` or `matched` → `declined` (with a reason). A matched request
may be matched again to another route. `fulfilled` and `declined` are final. Repeating a
move changes nothing.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from tests.route_requests.support import add_request, request_path
from tests.routes.support import add_company, add_route
from tests.staff.support import add_staff, audit_entries, signed_in

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
DECLINE = {"status": "declined", "decline_reason": "No van planned for this area."}


def status_path(request_id: uuid.UUID) -> str:
    return f"{request_path(request_id)}/status"


def _match(route_id: uuid.UUID) -> dict[str, str]:
    return {"status": "matched", "matched_route_id": str(route_id)}


async def test_a_request_is_matched_then_fulfilled(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    forming = await add_route(db_session, code="AYAT-02", status="forming")
    request = await add_request(db_session)
    matched = await api.put(
        status_path(request), json=_match(forming), headers=signed_in(member.id)
    )
    assert matched.status_code == 200
    assert matched.headers["Cache-Control"] == "no-store"
    assert (matched.json()["status"], matched.json()["matched_route"]) == (
        "matched",
        {"id": str(forming), "code": "AYAT-02"},
    )
    done = await api.put(
        status_path(request), json={"status": "fulfilled"}, headers=signed_in(member.id)
    )
    assert (done.json()["status"], done.json()["matched_route"]["id"]) == (
        "fulfilled",
        str(forming),
    )
    changes = [(e.action, e.before, e.after) for e in await audit_entries(db_session, request)]
    assert sorted(changes, key=str) == sorted(
        [
            (
                "route_request.status_changed",
                {"status": "new", "matched_route_id": None},
                {"status": "matched", "matched_route_id": str(forming)},
            ),
            (
                "route_request.status_changed",
                {"status": "matched"},
                {"status": "fulfilled"},
            ),
        ],
        key=str,
    )


async def test_a_request_is_fulfilled_directly(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session)
    response = await api.put(
        status_path(request), json={"status": "fulfilled"}, headers=signed_in(member_id)
    )
    assert (response.json()["status"], response.json()["matched_route"]) == ("fulfilled", None)


async def test_a_matched_request_is_matched_again(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The van it was matched to won't come; another route will take them."""
    member_id = (await add_staff(db_session)).id
    first, second = await add_route(db_session), await add_route(db_session, status="forming")
    request = await add_request(db_session, status="matched", matched_route_id=first)
    response = await api.put(
        status_path(request), json=_match(second), headers=signed_in(member_id)
    )
    assert response.json()["matched_route"]["id"] == str(second)
    (entry,) = await audit_entries(db_session, request)
    assert (entry.before, entry.after) == (
        {"matched_route_id": str(first)},
        {"matched_route_id": str(second)},
    )


@pytest.mark.parametrize("now", ["new", "matched"])
async def test_an_open_request_is_declined_with_a_reason(
    api: httpx.AsyncClient, db_session: AsyncSession, now: str
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session)
    values: dict[str, Any] = {"matched_route_id": route} if now == "matched" else {}
    request = await add_request(db_session, status=now, **values)
    body = {**DECLINE, "decline_reason": "  No van   planned. "}
    response = await api.put(status_path(request), json=body, headers=signed_in(member_id))
    assert (response.json()["status"], response.json()["decline_reason"]) == (
        "declined",
        "No van planned.",
    )
    (entry,) = await audit_entries(db_session, request)
    assert (entry.before, entry.after) == (
        {"status": now, "decline_reason": None},
        {"status": "declined", "decline_reason": REDACTED},  # free text: may name people
    )


@pytest.mark.parametrize(
    ("now", "body"),
    [
        ("matched", {"status": "new"}),
        ("fulfilled", {"status": "new"}),
        ("fulfilled", {"status": "matched"}),
        ("fulfilled", DECLINE),
        ("declined", {"status": "new"}),
        ("declined", {"status": "matched"}),
        ("declined", {"status": "fulfilled"}),
    ],
    ids=str,
)
async def test_other_moves_are_refused(
    api: httpx.AsyncClient, db_session: AsyncSession, now: str, body: dict[str, str]
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session)
    values: dict[str, Any] = {}
    if now in ("matched", "fulfilled"):
        values["matched_route_id"] = route
    if now == "declined":
        values["decline_reason"] = "Too far."
    if body["status"] == "matched":
        body = _match(route)
    request = await add_request(db_session, status=now, **values)
    response = await api.put(status_path(request), json=body, headers=signed_in(member_id))
    assert (response.status_code, response.json()["code"]) == (409, "INVALID_STATUS_CHANGE")
    assert await audit_entries(db_session, request) == []


@pytest.mark.parametrize("now", ["new", "matched", "fulfilled", "declined"])
async def test_repeating_a_move_changes_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession, now: str
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session)
    values: dict[str, Any] = {}
    body: dict[str, str] = {"status": now}
    if now == "matched":
        values["matched_route_id"] = route
        body = _match(route)
    if now == "declined":
        values["decline_reason"] = "Too far."
        body = {**DECLINE}  # another reason: still no change
    request = await add_request(db_session, status=now, **values)
    before = (await api.get(request_path(request), headers=signed_in(member_id))).json()
    response = await api.put(status_path(request), json=body, headers=signed_in(member_id))
    assert (response.status_code, response.json()) == (200, before)
    assert await audit_entries(db_session, request) == []


async def test_the_route_matched_must_fit_the_rider(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    company = await add_company(db_session)
    unfit = [
        uuid.uuid4(),
        await add_route(db_session, deleted_at=START),
        await add_route(
            db_session, route_type="corporate", company_id=company, launch_minimum=None
        ),
        await add_route(db_session, route_type="student", school_name="ICS"),  # for an adult
    ]
    adult = await add_request(db_session)
    for route in unfit:
        response = await api.put(
            status_path(adult), json=_match(route), headers=signed_in(member_id)
        )
        assert response.status_code == 422, route
        assert [e["field"] for e in response.json()["errors"]] == ["/matched_route_id"]
    student = await add_request(db_session, rider_type="student", school_name="Sandford")
    other_school = await add_route(db_session, route_type="student", school_name="ICS")
    for route in (other_school, await add_route(db_session)):
        response = await api.put(
            status_path(student), json=_match(route), headers=signed_in(member_id)
        )
        assert [e["field"] for e in response.json()["errors"]] == ["/matched_route_id"]
    same_school = await add_route(db_session, route_type="student", school_name="SANDFORD")
    response = await api.put(
        status_path(student), json=_match(same_school), headers=signed_in(member_id)
    )
    assert response.json()["status"] == "matched"


async def test_the_route_named_is_judged_before_the_move(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """What a body names first, as on create: an unknown route is a 422 even when the move
    itself would be refused."""
    member_id = (await add_staff(db_session)).id
    done = await add_request(db_session, status="fulfilled")
    response = await api.put(
        status_path(done), json=_match(uuid.uuid4()), headers=signed_in(member_id)
    )
    assert [e["field"] for e in response.json()["errors"]] == ["/matched_route_id"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"status": "matched"}, "/matched_route_id"),
        ({"status": "fulfilled", "matched_route_id": str(uuid.UUID(int=1))}, "/matched_route_id"),
        ({"status": "declined"}, "/decline_reason"),
        ({"status": "declined", "decline_reason": "   "}, "/decline_reason"),
        ({"status": "declined", "decline_reason": "x" * 1001}, "/decline_reason"),
        ({"status": "fulfilled", "decline_reason": "Too far."}, "/decline_reason"),
        ({"status": "closed"}, "/status"),
        ({}, "/status"),
        ({"status": "fulfilled", "note": "x"}, "/note"),
    ],
    ids=str,
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, str], field: str
) -> None:
    member_id = (await add_staff(db_session)).id
    request = await add_request(db_session)
    response = await api.put(status_path(request), json=body, headers=signed_in(member_id))
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == [field]


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_requests_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    target = await add_request(db_session, deleted_at=START) if deleted else uuid.uuid4()
    response = await api.put(
        status_path(target), json={"status": "fulfilled"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.put(
        status_path(uuid.UUID(int=1)), json={"status": "fulfilled"}, headers=signed_in(uuid.uuid4())
    )
    assert response.status_code == 503
