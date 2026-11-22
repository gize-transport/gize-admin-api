"""GZ-083: the routes list filtered to those ready to launch (`ready_to_launch=true`).

The overview's "ready to launch" count opens this list: the same routes, by the same rule
(forming, with a launch minimum, reached by the riders reserved for the next launch day).
`false` lists every other live route. The filter combines with the others, the sorts and
paging. Sign-in, roles and a database failure are covered here with the filter on.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import schedule
from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.routes.models import AssignmentStatus, Route, RouteStatus
from tests.routes.support import ROUTES, add_company, add_route, add_seat
from tests.staff.support import add_admin, add_staff, signed_in
from tests.support import every_page

DECEMBER = date(2026, 12, 1)  # the next launch day from 15 November
FORMING = RouteStatus.FORMING


async def _forming(session: AsyncSession, code: str, minimum: int, *seats: date) -> str:
    """A forming route with riders reserved from each of `seats`."""
    route = await add_route(session, code=code, status=FORMING, launch_minimum=minimum)
    for start in seats:
        await add_seat(session, route, start_date=start)
    return str(route)


async def _delete(session: AsyncSession, route: str) -> None:
    async with session.begin():
        await session.execute(
            update(Route).where(Route.id == uuid.UUID(route)).values(deleted_at=datetime.now(UTC))
        )


async def _overview_count(api: httpx.AsyncClient, member: uuid.UUID) -> int:
    response = await api.get("/v1/overview", headers=signed_in(member))
    count: int = response.json()["routes"]["ready_to_launch"]
    return count


async def test_the_list_shows_exactly_the_routes_the_overview_counts(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    ready = await _forming(db_session, "RDY-01", 2, DECEMBER, date(2026, 11, 1))
    over = await _forming(db_session, "RDY-02", 1, DECEMBER, DECEMBER)
    others = [
        await _forming(db_session, "NOT-01", 2, DECEMBER),  # one short
        await _forming(db_session, "NOT-02", 1, date(2027, 1, 1)),  # reserved for later
        await _forming(db_session, "NOT-03", 1),  # no one yet
    ]
    # Reached, but not a forming route: a draft and a running one.
    for code, status in (("NOT-04", RouteStatus.DRAFT), ("NOT-05", RouteStatus.ACTIVE)):
        route = await add_route(db_session, code=code, status=status, launch_minimum=1)
        await add_seat(db_session, route, start_date=DECEMBER)
        others.append(str(route))
    # A company's route has no launch minimum: it starts without a launch.
    company = await add_company(db_session)
    corporate = await add_route(db_session, code="NOT-06", route_type="corporate",
                                company_id=company, launch_minimum=None)  # fmt: skip
    await add_seat(db_session, corporate, start_date=DECEMBER)
    # Reached by one who left, or one gone before the launch day: not counted.
    left = await _forming(db_session, "NOT-07", 1)
    await add_seat(db_session, uuid.UUID(left), status=AssignmentStatus.ENDED)
    gone = await _forming(db_session, "NOT-08", 1)
    await add_seat(db_session, uuid.UUID(gone), start_date=date(2026, 11, 1),
                   end_date=date(2026, 11, 30))  # fmt: skip
    others += [str(corporate), left, gone]
    # Deleted: never listed.
    deleted = await _forming(db_session, "DEL-01", 1, DECEMBER)
    await _delete(db_session, deleted)
    headers = signed_in(member)

    listed = await every_page(api, ROUTES, headers, ready_to_launch="true", sort="code")
    assert listed == [ready, over]
    assert len(listed) == await _overview_count(api, member)
    rest = await every_page(api, ROUTES, headers, ready_to_launch="false", sort="code")
    assert rest == others
    assert set(await every_page(api, ROUTES, headers)) == {*listed, *rest}


@pytest.mark.parametrize(
    ("today", "start", "ready"),
    [
        (date(2026, 11, 15), date(2027, 1, 1), False),
        (date(2026, 12, 1), date(2027, 1, 1), False),
        (date(2026, 12, 2), date(2027, 1, 1), True),
        (date(2026, 12, 1), date(2026, 12, 1), True),
    ],
)
async def test_the_launch_day_moves_on_after_the_1st(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    today: date,
    start: date,
    ready: bool,
) -> None:
    """Reserved from 1 January: not ready while the next launch is 1 December (on the 1st
    the launch day is that day), ready once it is 1 January. Reserved from 1 December:
    ready on 1 December itself."""
    member = (await add_staff(db_session)).id
    route = await _forming(db_session, "RDY-01", 1, start)
    monkeypatch.setattr(schedule, "addis_today", lambda: today)
    headers = signed_in(member)
    assert await every_page(api, ROUTES, headers, ready_to_launch="true") == ([route] * ready)
    assert await every_page(api, ROUTES, headers, ready_to_launch="false") == (
        [] if ready else [route]
    )
    assert await _overview_count(api, member) == int(ready)


async def test_the_filter_combines_with_the_others_and_every_sort(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = (await add_staff(db_session)).id
    made = [await _forming(db_session, f"RDY-{n:02}", 1, DECEMBER) for n in range(1, 6)]
    await _forming(db_session, "NOT-01", 2, DECEMBER)
    headers = signed_in(member)
    assert await every_page(api, ROUTES, headers, ready_to_launch="true", sort="code") == made
    newest = await every_page(api, ROUTES, headers, ready_to_launch="true")
    assert sorted(newest) == sorted(made)
    by_time = await every_page(api, ROUTES, headers, ready_to_launch="true", sort="morning_time")
    assert sorted(by_time) == sorted(made)
    filtered = await every_page(
        api, ROUTES, headers, ready_to_launch="true", q="rdy-0", status="forming", sort="code"
    )
    assert filtered == made
    for status in ("draft", "active", "paused"):
        assert await every_page(api, ROUTES, headers, ready_to_launch="true", status=status) == []


@pytest.mark.parametrize("value", ["maybe", "", "2", "yes please"])
async def test_a_bad_value_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, value: str
) -> None:
    member = (await add_staff(db_session)).id
    response = await api.get(ROUTES, params={"ready_to_launch": value}, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert response.json()["errors"][0]["field"] == "/ready_to_launch"


async def test_callers_must_be_signed_in_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    params = {"ready_to_launch": "true"}
    response = await api.get(ROUTES, params=params)
    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")
    gone = (await add_admin(db_session, is_active=False)).id
    for caller in (uuid.uuid4(), gone):
        response = await api.get(ROUTES, params=params, headers=signed_in(caller))
        assert response.status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(
        ROUTES, params={"ready_to_launch": "true"}, headers=signed_in(uuid.uuid4())
    )
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
