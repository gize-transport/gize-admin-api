"""GZ-021: routes, created, listed, read and edited by staff.

A route is one van on a corridor: public (adults), student (one school) or corporate (one
company). Type is fixed at creation (owner decision). Status and prices have their own
tickets (GZ-024, GZ-022). Seat counts are in test_seats.py.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode
from gize_api.main import GizeAPI, create_app
from gize_api.routes import service
from gize_api.routes.models import Route, RouteStatus, RouteType
from tests.factories import CorridorFactory
from tests.routes.support import ROUTES, add_company, add_corridor, add_route, route_path
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


def _public(corridor_id: uuid.UUID, **changes: object) -> dict[str, object]:
    return {
        "code": "bole-mex-01",
        "corridor_id": str(corridor_id),
        "route_type": "public",
        "morning_time": "06:30",
        "return_time": "17:30",
        "seats": 14,
        "launch_minimum": 10,
        **changes,
    }


# Create


async def test_staff_create_a_public_route(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session, "Bole", "Mexico")
    response = await api.post(ROUTES, json=_public(corridor), headers=with_key(member.id))
    assert response.status_code == 201
    route = response.json()
    assert route == {
        "id": route["id"],
        "code": "BOLE-MEX-01",  # stored upper-case
        "corridor": {
            "id": str(corridor),
            "origin_area": route["corridor"]["origin_area"],
            "destination_area": route["corridor"]["destination_area"],
        },
        "route_type": "public",
        "school_name": None,
        "company": None,
        "morning_time": "06:30",  # local Addis Ababa time
        "return_time": "17:30",
        "seats": 14,
        "launch_minimum": 10,
        "prices": {"round_trip": None, "one_way": None, "saturday": None},
        "status": "draft",
        "copied_from_route_id": None,
        "seats_taken": {"this_month": 0, "next_month": 0},
        "seats_free": {"this_month": 14, "next_month": 14},
        "reserved": 0,
        "created_at": route["created_at"],
        "updated_at": route["updated_at"],
    }
    assert route["corridor"]["origin_area"]["name"] == "Bole"
    assert response.headers["Location"] == f"{ROUTES}/{route['id']}"
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = await audit_entries(db_session, uuid.UUID(route["id"]))
    assert (entry.action, entry.actor_staff_id, entry.before) == ("route.created", member.id, None)
    assert entry.after is not None
    assert (entry.after["code"], entry.after["morning_time"], entry.after["status"]) == (
        "BOLE-MEX-01",
        "06:30:00",
        "draft",
    )


async def test_a_student_route_names_its_school(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    body = _public(corridor, route_type="student", school_name="  Lycée  Guebre-Mariam ")
    response = await api.post(ROUTES, json=body, headers=with_key(member.id))
    assert response.status_code == 201
    assert response.json()["school_name"] == "Lycée Guebre-Mariam"


async def test_a_corporate_route_belongs_to_a_company_and_has_no_minimum_or_prices(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    company = await add_company(db_session, name="Acme Ethiopia")
    body = _public(corridor, route_type="corporate", company_id=str(company))
    del body["launch_minimum"]
    response = await api.post(ROUTES, json=body, headers=with_key(member.id))
    assert response.status_code == 201
    route = response.json()
    assert route["company"] == {"id": str(company), "name": "Acme Ethiopia"}
    assert (route["launch_minimum"], route["prices"]) == (None, None)


@pytest.mark.parametrize(
    "change",
    [
        {"school_name": "Lycée"},  # only student routes have a school
        {"company_id": str(uuid.UUID(int=1))},  # only corporate routes have a company
        {"launch_minimum": None},
        {"route_type": "student"},  # no school
        {"route_type": "corporate", "launch_minimum": None},  # no company
        {"route_type": "corporate", "company_id": str(uuid.UUID(int=1))},  # has a minimum
        {"route_type": "shuttle"},
    ],
)
async def test_fields_that_dont_fit_the_type_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, object]
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    body = {k: v for k, v in _public(corridor, **change).items() if v is not None}
    response = await api.post(ROUTES, json=body, headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("problem", ["unknown", "deleted", "inactive"])
async def test_the_company_must_be_active(
    api: httpx.AsyncClient, db_session: AsyncSession, problem: str
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    company = (
        uuid.uuid4()
        if problem == "unknown"
        else await add_company(
            db_session, **({"deleted_at": START} if problem == "deleted" else {"is_active": False})
        )
    )
    body = _public(corridor, route_type="corporate", company_id=str(company))
    del body["launch_minimum"]
    response = await api.post(ROUTES, json=body, headers=with_key(member.id))
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/company_id"]


@pytest.mark.parametrize("deleted", [False, True])
async def test_the_corridor_must_exist(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member = await add_staff(db_session)
    corridor = uuid.uuid4()
    if deleted:
        async with db_session.begin():
            corridor = (await CorridorFactory().create(db_session, deleted_at=START)).id
    response = await api.post(ROUTES, json=_public(corridor), headers=with_key(member.id))
    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["/corridor_id"]


async def test_a_code_in_use_is_409_and_a_deleted_routes_code_is_free(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    corridor = await add_corridor(db_session)
    await add_route(db_session, code="BOLE-MEX-01")
    await add_route(db_session, code="BOLE-MEX-02", deleted_at=START)
    taken = await api.post(ROUTES, json=_public(corridor), headers=with_key(member_id))
    assert (taken.status_code, taken.json()["code"]) == (409, "ROUTE_CODE_TAKEN")
    reused = _public(corridor, code="BOLE-MEX-02")
    assert (await api.post(ROUTES, json=reused, headers=with_key(member_id))).status_code == 201


async def test_a_retried_create_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    headers = with_key(member.id)
    first = await api.post(ROUTES, json=_public(corridor), headers=headers)
    again = await api.post(ROUTES, json=_public(corridor), headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert (again.json(), again.headers["Idempotent-Replayed"]) == (first.json(), "true")
    missing = await api.post(ROUTES, json=_public(corridor), headers=signed_in(member.id))
    assert missing.status_code == 428


@pytest.mark.parametrize(
    "change",
    [
        {"code": "A"},
        {"code": "A" * 21},
        {"code": "BOLE MEX"},
        {"code": "-BOLE"},
        {"code": "BOLE-"},
        {"code": "BOLÉ-1"},
        {"morning_time": "6:30"},
        {"morning_time": "24:00"},
        {"morning_time": "06:30:00"},
        {"morning_time": "06:60"},
        {"morning_time": "18:00"},  # not before the return
        {"return_time": "06:30"},  # same as the morning
        {"seats": 0},
        {"seats": 61},
        {"seats": "14"},
        {"launch_minimum": 0},
        {"launch_minimum": 15},  # more than the seats
        {"status": "active"},  # GZ-024
        {"price_round_trip": 1500},  # GZ-022, admins only
        {"prices": {"round_trip": 1500}},
        {"surprise": 1},
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, object]
) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    response = await api.post(ROUTES, json=_public(corridor, **change), headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# List and read


async def test_staff_list_routes_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    older = await add_route(db_session, code="OLD-01", created_at=START)
    newer = await add_route(db_session, code="NEW-01", created_at=START + timedelta(1))
    await add_route(db_session, code="GONE-01", deleted_at=START, created_at=START + timedelta(2))
    response = await api.get(ROUTES, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [item["id"] for item in response.json()["items"]] == [str(newer), str(older)]


async def test_filters(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    corridor = await add_corridor(db_session)
    company = await add_company(db_session)
    on_corridor = await add_route(db_session, code="BOLE-01", corridor_id=corridor)
    active = await add_route(db_session, code="BOLE-02", status=RouteStatus.ACTIVE)
    student = await add_route(db_session, code="SCH-01", route_type="student", school_name="ICS")
    corporate = await add_route(
        db_session, code="ACME-01", route_type="corporate", company_id=company, launch_minimum=None
    )

    async def found(**params: str) -> set[str]:
        response = await api.get(ROUTES, params=params, headers=signed_in(member.id))
        return {item["id"] for item in response.json()["items"]}

    assert await found(corridor_id=str(corridor)) == {str(on_corridor)}
    assert await found(status="active") == {str(active)}
    assert await found(route_type="student") == {str(student)}
    assert await found(company_id=str(company)) == {str(corporate)}
    assert await found(q="bole") == {str(on_corridor), str(active)}  # code prefix
    assert await found(q="bole-0", status="draft") == {str(on_corridor)}


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    made = [await add_route(db_session, created_at=START + timedelta(minutes=i)) for i in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(ROUTES, params=params, headers=signed_in(member.id))).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert (seen, cursor) == ([str(r) for r in reversed(made)], None)


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Corridor, areas, company and seat counts come with the page: no query per route."""
    member = await add_staff(db_session)
    company = await add_company(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        for _ in range(rows):
            await add_route(
                db_session, route_type="corporate", company_id=company, launch_minimum=None
            )
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(ROUTES, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts[0] == counts[1] <= 4


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"status": "gone"}, "VALIDATION_FAILED"),
        ({"route_type": "shuttle"}, "VALIDATION_FAILED"),
        ({"corridor_id": "nope"}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_list_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    member = await add_staff(db_session)
    response = await api.get(ROUTES, params=params, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_staff_read_one_route(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, code="BOLE-01")
    response = await api.get(route_path(route), headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (200, "BOLE-01")


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_routes_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    member_id = (await add_staff(db_session)).id
    target = await add_route(db_session, deleted_at=START) if deleted else uuid.uuid4()
    for method in ("GET", "PATCH"):
        response = await api.request(
            method, route_path(target), json={"seats": 12}, headers=signed_in(member_id)
        )
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), method


# Edit


async def test_staff_edit_a_route(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, code="BOLE-01", seats=14, launch_minimum=10)
    body = {"code": "bole-02", "morning_time": "06:45", "seats": 18, "launch_minimum": 12}
    response = await api.patch(route_path(route), json=body, headers=signed_in(member.id))
    assert response.status_code == 200
    changed = response.json()
    assert (changed["code"], changed["morning_time"], changed["seats"]) == ("BOLE-02", "06:45", 18)
    assert changed["seats_free"] == {"this_month": 18, "next_month": 18}
    (entry,) = await audit_entries(db_session, route)
    assert (entry.action, entry.before, entry.after) == (
        "route.updated",
        {"code": "BOLE-01", "morning_time": "06:30:00", "seats": 14, "launch_minimum": 10},
        {"code": "BOLE-02", "morning_time": "06:45:00", "seats": 18, "launch_minimum": 12},
    )


async def test_an_edit_to_what_is_there_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session, code="BOLE-01", seats=14)
    body = {"code": "bole-01", "seats": 14, "return_time": "17:30"}
    response = await api.patch(route_path(route), json=body, headers=signed_in(member.id))
    assert response.status_code == 200
    assert await audit_entries(db_session, route) == []


async def test_a_student_route_changes_school_and_a_corporate_route_company(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    student = await add_route(db_session, route_type="student", school_name="ICS")
    first, second = await add_company(db_session), await add_company(db_session)
    corporate = await add_route(
        db_session, route_type="corporate", company_id=first, launch_minimum=None
    )
    school = await api.patch(
        route_path(student), json={"school_name": "Sandford"}, headers=signed_in(member.id)
    )
    assert (school.status_code, school.json()["school_name"]) == (200, "Sandford")
    company = await api.patch(
        route_path(corporate), json={"company_id": str(second)}, headers=signed_in(member.id)
    )
    assert (company.status_code, company.json()["company"]["id"]) == (200, str(second))


@pytest.mark.parametrize(
    ("route_type", "body"),
    [
        ("public", {"route_type": "student"}),  # fixed at creation
        ("public", {"school_name": "ICS"}),
        ("public", {"company_id": str(uuid.UUID(int=1))}),
        ("corporate", {"launch_minimum": 3}),
        ("public", {"morning_time": "18:00"}),  # after the stored return
        ("public", {"seats": 12, "launch_minimum": 13}),
        ("public", {}),
        ("public", {"seats": None}),
        ("public", {"status": "active"}),
        ("public", {"price_one_way": 900}),
    ],
)
async def test_invalid_edits_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, route_type: str, body: dict[str, object]
) -> None:
    member = await add_staff(db_session)
    extra: dict[str, object] = {}
    if route_type == "corporate":
        extra = {"company_id": await add_company(db_session), "launch_minimum": None}
    route = await add_route(db_session, route_type=route_type, **extra)
    response = await api.patch(route_path(route), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_an_inactive_company_is_refused_on_edit(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    first = await add_company(db_session)
    sleeping = await add_company(db_session, is_active=False)
    route = await add_route(
        db_session, route_type="corporate", company_id=first, launch_minimum=None
    )
    response = await api.patch(
        route_path(route), json={"company_id": str(sleeping)}, headers=signed_in(member.id)
    )
    assert [e["field"] for e in response.json()["errors"]] == ["/company_id"]


async def test_the_launch_minimum_and_seats_agree(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    route = await add_route(db_session, seats=14, launch_minimum=10)
    fewer = await api.patch(route_path(route), json={"seats": 9}, headers=signed_in(member_id))
    assert (fewer.status_code, fewer.json()["code"]) == (409, "SEATS_BELOW_LAUNCH_MINIMUM")
    more = await api.patch(
        route_path(route), json={"launch_minimum": 15}, headers=signed_in(member_id)
    )
    assert (more.status_code, more.json()["code"]) == (409, "SEATS_BELOW_LAUNCH_MINIMUM")


async def test_renaming_to_a_code_in_use_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    await add_route(db_session, code="BOLE-01")
    route = await add_route(db_session, code="BOLE-02")
    response = await api.patch(
        route_path(route), json={"code": "Bole-01"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "ROUTE_CODE_TAKEN")


async def test_moving_to_another_corridor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    route = await add_route(db_session)
    other = await add_corridor(db_session, "Ayat", "Piassa")
    response = await api.patch(
        route_path(route), json={"corridor_id": str(other)}, headers=signed_in(member.id)
    )
    assert response.status_code == 200
    assert response.json()["corridor"]["destination_area"]["name"] == "Piassa"


# Who may call


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", ROUTES),
        ("GET", ROUTES),
        ("GET", f"{ROUTES}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{ROUTES}/{uuid.UUID(int=1)}"),
    ],
)
async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    body = {"seats": 12}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert (await api.request(method, path, json=body, headers=key)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        headers = {**signed_in(caller), **key}
        assert (await api.request(method, path, json=body, headers=headers)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", ROUTES),
        ("GET", ROUTES),
        ("GET", f"{ROUTES}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{ROUTES}/{uuid.UUID(int=1)}"),
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
    body = _public(uuid.UUID(int=1))
    response = await api.request(method, path, json=body, headers=with_key(uuid.uuid4()))
    assert response.status_code == 503


def test_routes_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection, member = schema["paths"][ROUTES], schema["paths"][f"{ROUTES}/{{route_id}}"]
    assert {op["operationId"] for op in collection.values()} == {"routes_create", "routes_list"}
    assert {op["operationId"] for op in member.values()} == {"routes_get", "routes_update"}
    assert collection["post"]["responses"]["409"]["x-error-codes"] == [
        "IDEMPOTENCY_IN_PROGRESS",
        "ROUTE_CODE_TAKEN",
    ]
    assert member["patch"]["responses"]["409"]["x-error-codes"] == [
        "ROUTE_CODE_TAKEN",
        "ROUTE_HAS_RIDERS",
        "SEATS_BELOW_LAUNCH_MINIMUM",
        "SEATS_BELOW_TAKEN",
    ]
    schemas = schema["components"]["schemas"]
    time_field = schemas["RouteCreate"]["properties"]["morning_time"]
    assert time_field["type"] == "string"
    assert time_field["pattern"] == "^([01][0-9]|2[0-3]):[0-5][0-9]$"
    assert "route_type" not in schemas["RouteUpdate"]["properties"]
    assert schemas["RouteStatus"]["enum"] == ["draft", "forming", "active", "paused"]


async def test_an_empty_list_is_an_empty_page(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    response = await api.get(ROUTES, params={"q": "NOTHING"}, headers=signed_in(member.id))
    assert response.json() == {"items": [], "next_cursor": None}


async def test_other_constraint_errors_are_not_reported_as_a_code_taken(
    db_session: AsyncSession,
) -> None:
    """Only the code index's violation becomes ROUTE_CODE_TAKEN; the table's own checks stay
    errors (a bug the schema should have caught)."""
    await add_route(db_session, code="BOLE-01")
    corridor = await add_corridor(db_session)

    async def flush_new(**values: object) -> None:
        async with db_session.begin():
            fields = {
                "code": "BOLE-02",
                "corridor_id": corridor,
                "route_type": RouteType.PUBLIC,
                "morning_time": time(6, 30),
                "return_time": time(17, 30),
                "seats": 14,
                "launch_minimum": 10,
                **values,
            }
            db_session.add(Route(**fields))
            await service._flush(db_session)

    with pytest.raises(IntegrityError):
        await flush_new(seats=61)
    with pytest.raises(AppError) as taken:
        await flush_new(code="BOLE-01")
    assert taken.value.code is ErrorCode.ROUTE_CODE_TAKEN


async def test_the_database_refuses_an_individual_route_without_a_minimum(
    db_session: AsyncSession,
) -> None:
    """A NULL must not slip through the check (`x BETWEEN 1 AND seats` is NULL, not false)."""
    corridor = await add_corridor(db_session)

    async def insert(route_type: RouteType, **extra: object) -> None:
        async with db_session.begin():
            db_session.add(
                Route(
                    code=f"NOMIN-{route_type.value[:3].upper()}",
                    corridor_id=corridor,
                    route_type=route_type,
                    morning_time=time(6, 30),
                    return_time=time(17, 30),
                    seats=14,
                    launch_minimum=None,
                    **extra,
                )
            )
            await db_session.flush()

    with pytest.raises(IntegrityError):
        await insert(RouteType.PUBLIC)
    with pytest.raises(IntegrityError):
        await insert(RouteType.STUDENT, school_name="ICS")
