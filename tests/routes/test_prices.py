"""GZ-022: route prices, set by admins only.

Whole birr a month: round trip and one-way, and an optional Saturday add-on on public
routes. Corporate routes have none (companies are billed per agreement). Staff can't change
prices through any endpoint.
"""

import itertools
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.routes.models import Route
from tests.routes.support import ROUTES, add_company, add_route, route_path
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
PRICES = {"round_trip": 2400, "one_way": 1400, "saturday": 300}
WRITES = {"post", "put", "patch", "delete"}
# What a staff write naming a price may get: refused, never done, limited or broken.
REFUSED = {403, 404, 405, 409, 422, 428}


def _prices_path(route_id: uuid.UUID) -> str:
    return f"{route_path(route_id)}/prices"


async def _stored(session: AsyncSession, route_id: uuid.UUID) -> tuple[int | None, ...]:
    async with session.begin():
        route = await session.get_one(Route, route_id)
        return (route.price_round_trip, route.price_one_way, route.price_saturday)


async def test_admins_set_a_public_routes_prices(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    route = await add_route(db_session)
    response = await api.put(_prices_path(route), json=PRICES, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["prices"] == PRICES
    assert await _stored(db_session, route) == (2400, 1400, 300)
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.actor_staff_id) == ("route.prices_changed", admin.id)
    assert (entry.before, entry.after) == (
        {"price_round_trip": None, "price_one_way": None, "price_saturday": None},
        {"price_round_trip": 2400, "price_one_way": 1400, "price_saturday": 300},
    )


@pytest.mark.parametrize("saturday", [None, "left out"])
async def test_no_saturday_price_means_no_saturday_add_on(
    api: httpx.AsyncClient, db_session: AsyncSession, saturday: object
) -> None:
    admin = await add_admin(db_session)
    route = await add_route(
        db_session, price_round_trip=2000, price_one_way=1200, price_saturday=250
    )
    body = {"round_trip": 2000, "one_way": 1200}
    if saturday is None:
        body["saturday"] = None  # type: ignore[assignment]
    response = await api.put(_prices_path(route), json=body, headers=signed_in(admin.id))
    assert response.json()["prices"] == {"round_trip": 2000, "one_way": 1200, "saturday": None}
    (entry,) = await audit_entries(db_session, route)
    assert (entry.before, entry.after) == ({"price_saturday": 250}, {"price_saturday": None})


async def test_a_student_route_has_no_saturday_add_on(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    route = await add_route(db_session, route_type="student", school_name="ICS")
    without = {"round_trip": 1800, "one_way": 1000}
    assert (
        await api.put(_prices_path(route), json=without, headers=signed_in(admin.id))
    ).status_code == 200
    response = await api.put(_prices_path(route), json=PRICES, headers=signed_in(admin.id))
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/saturday"]


async def test_a_corporate_route_has_no_prices(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    company = await add_company(db_session)
    route = await add_route(
        db_session, route_type="corporate", company_id=company, launch_minimum=None
    )
    response = await api.put(_prices_path(route), json=PRICES, headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_HAS_NO_PRICES")
    assert await _stored(db_session, route) == (None, None, None)


async def test_setting_the_same_prices_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    route = await add_route(
        db_session, price_round_trip=2400, price_one_way=1400, price_saturday=300
    )
    response = await api.put(_prices_path(route), json=PRICES, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert await audit_entries(db_session, route) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"round_trip": 2400},
        {"one_way": 1400},
        {**PRICES, "round_trip": 0},
        {**PRICES, "one_way": 100_001},
        {**PRICES, "saturday": 0},
        {**PRICES, "round_trip": 2400.5},
        {**PRICES, "round_trip": "2400"},
        {**PRICES, "round_trip": None},
        {**PRICES, "currency": "ETB"},
        [],
    ],
)
async def test_invalid_prices_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    admin = await add_admin(db_session)
    route = await add_route(db_session)
    response = await api.put(_prices_path(route), json=body, headers=signed_in(admin.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    admin_id = (await add_admin(db_session)).id
    target = await add_route(db_session, deleted_at=START) if deleted else uuid.uuid4()
    response = await api.put(_prices_path(target), json=PRICES, headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")


async def test_staff_cannot_set_prices(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    response = await api.put(_prices_path(route), json=PRICES, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (403, "FORBIDDEN")
    assert await _stored(db_session, route) == (None, None, None)


async def test_no_route_endpoint_lets_staff_change_prices(
    api: httpx.AsyncClient, api_app: GizeAPI, db_session: AsyncSession
) -> None:
    """Every write under /v1/routes, called by staff, with each way of naming a price on its
    own (one field accepted by mistake must show): every call is refused, and no route,
    including any a write might create, has a price but the seeded one, unchanged. Endpoints
    added later are covered by walking the contract.

    A write that creates a route (create, copy) gets a valid body with the price merged in,
    and must be refused for the price field alone: the body is otherwise accepted."""
    member = await add_staff(db_session)
    route = await add_route(
        db_session, price_round_trip=2400, price_one_way=1400, price_saturday=300
    )
    bodies: list[dict[str, object]] = [
        {name: 1}
        for name in (
            "round_trip",
            "one_way",
            "saturday",
            "price_round_trip",
            "price_one_way",
            "price_saturday",
        )
    ]
    bodies += [{"prices": {"round_trip": 1, "one_way": 1}}, {"round_trip": 1, "one_way": 1}]
    async with db_session.begin():
        corridor_id = (await db_session.get_one(Route, route)).corridor_id
    codes = (f"WALK-{n:02d}" for n in itertools.count(1))
    bases: dict[tuple[str, str], Callable[[], dict[str, object]]] = {
        ("POST", ROUTES): lambda: {
            "code": next(codes), "corridor_id": str(corridor_id), "route_type": "public",
            "morning_time": "06:30", "morning_arrival_time": "07:15", "return_time": "17:30",
            "return_arrival_time": "18:15", "seats": 14, "launch_minimum": 10,
        },
        ("POST", f"{route_path(route)}/copies"): lambda: {
            "code": next(codes), "seats": 12, "launch_minimum": 8,
        },
    }  # fmt: skip
    writes = []
    for path, item in api_app.openapi()["paths"].items():
        if not path.startswith(ROUTES):
            continue
        filled = path.replace("{route_id}", str(route))
        filled = re.sub(r"\{[a-z_]+\}", lambda _: str(uuid.uuid4()), filled)
        assert "{" not in filled, path
        writes += [(method.upper(), filled) for method in item if method in WRITES]
    assert ("PUT", _prices_path(route)) in writes  # the walk sees the prices endpoint
    assert set(bases) <= set(writes)  # and both writes that create a route
    for method, path in writes:
        for body in bodies:
            base = bases.get((method, path))
            sent = body if base is None else {**base(), **body}
            response = await api.request(method, path, json=sent, headers=with_key(member.id))
            assert response.status_code in REFUSED, (method, path, body, response.status_code)
            if base is not None:  # refused for the price fields, nothing else
                named = {e["field"].split("/")[1] for e in response.json()["errors"]}
                assert named <= set(body), (path, body, named)
    async with db_session.begin():
        priced = await db_session.execute(
            select(
                Route.id, Route.price_round_trip, Route.price_one_way, Route.price_saturday
            ).where(
                or_(
                    Route.price_round_trip.is_not(None),
                    Route.price_one_way.is_not(None),
                    Route.price_saturday.is_not(None),
                )
            )
        )
        assert [tuple(row) for row in priced] == [(route, 2400, 1400, 300)]


async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    path = _prices_path(uuid.UUID(int=1))
    assert (await api.put(path, json=PRICES)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        assert (await api.put(path, json=PRICES, headers=signed_in(caller))).status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.put(
        _prices_path(uuid.UUID(int=1)), json=PRICES, headers=signed_in(uuid.uuid4())
    )
    assert response.status_code == 503


def test_prices_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][f"{ROUTES}/{{route_id}}/prices"]["put"]
    assert operation["operationId"] == "routes_set_prices"
    assert operation["responses"]["409"]["x-error-codes"] == ["ROUTE_HAS_NO_PRICES"]
    assert operation["responses"]["404"]["x-error-codes"] == ["NOT_FOUND"]
    body = schema["components"]["schemas"]["RoutePricesSet"]
    assert set(body["required"]) == {"round_trip", "one_way"}
    assert body["properties"]["round_trip"]["maximum"] == 100_000
