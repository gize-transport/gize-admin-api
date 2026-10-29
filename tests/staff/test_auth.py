"""GZ-011: `current_staff` and `require_role`, on test routes.

Who may call is decided by our staff table, never by the token: a valid Supabase token
for someone who is not active staff gets 403, and the role is always the table's.
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.ratelimit import STAFF, RateLimiter
from gize_api.core.security import (
    CurrentStaff,
    current_staff,
    require_role,
    staff_rate_limit,
    staff_router,
)
from gize_api.main import GizeAPI, create_app
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.support import UNREACHABLE_REDIS_URL, SettingsFactory
from tests.tokens import bearer, make_token


@pytest.fixture(autouse=True)
def _fresh_sentry_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security._reported_at", {})


@pytest.fixture
def test_routes(api_app: GizeAPI) -> GizeAPI:
    router = staff_router(prefix="/_test")

    @router.get("/whoami")
    async def whoami(staff: CurrentStaff = Depends(current_staff)) -> dict[str, str]:  # noqa: B008
        return {"id": str(staff.id), "role": staff.role.value}

    @router.get("/admin-only")
    async def admin_only(
        staff: CurrentStaff = Depends(require_role(StaffRole.ADMIN)),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": staff.role.value}

    @router.get("/either")
    async def either(
        staff: CurrentStaff = Depends(require_role(StaffRole.ADMIN, StaffRole.STAFF)),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": staff.role.value}

    api_app.include_router(router)
    return api_app


@pytest.fixture
async def http(test_routes: GizeAPI, api: httpx.AsyncClient) -> httpx.AsyncClient:
    return api


async def _staff(db_session: AsyncSession, **overrides: object) -> Staff:
    async with db_session.begin():
        return await StaffFactory().create(db_session, **overrides)


def _as(staff: Staff, **claims: Any) -> dict[str, str]:  # noqa: ANN401 - claims
    return bearer(make_token(sub=str(staff.id), **claims))


def _events(output: str, event: str) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [line for line in lines if line["event"] == event]


async def test_active_staff_get_through_with_their_role(
    http: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session)
    response = await http.get("/_test/whoami", headers=_as(staff))
    assert response.status_code == 200
    assert response.json() == {"id": str(staff.id), "role": "staff"}


async def test_no_token_is_401(http: httpx.AsyncClient) -> None:
    response = await http.get("/_test/whoami")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


async def test_a_bad_token_is_401(http: httpx.AsyncClient, db_session: AsyncSession) -> None:
    staff = await _staff(db_session)
    response = await http.get("/_test/whoami", headers=_as(staff, aud="anon"))
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("active", "reason"),
    [(None, "not_staff"), (False, "inactive")],
)
async def test_a_valid_token_without_active_staff_is_403(
    http: httpx.AsyncClient,
    db_session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    active: bool | None,
    reason: str,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "gize_api.core.logging.sentry_sdk.capture_message", lambda event, **_: sent.append(event)
    )
    if active is None:
        headers = bearer(make_token(sub=str(uuid.uuid4())))  # a Supabase user, not staff
    else:
        headers = _as(await _staff(db_session, is_active=active))
    for _ in range(2):
        response = await http.get("/_test/whoami", headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"
    events = _events(capsys.readouterr().err, "staff_access_denied")
    assert [event["reason"] for event in events] == [reason, reason]
    assert sent == ["staff_access_denied"]  # Sentry once a minute per reason


async def test_the_role_comes_from_our_table_not_the_token(
    http: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session, role=StaffRole.STAFF)
    headers = _as(staff, app_metadata={"role": "admin"}, user_role="admin")
    assert (await http.get("/_test/whoami", headers=headers)).json()["role"] == "staff"
    assert (await http.get("/_test/admin-only", headers=headers)).status_code == 403


async def test_require_role_lets_the_named_roles_through(
    http: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _staff(db_session, role=StaffRole.ADMIN)
    staff = await _staff(db_session, role=StaffRole.STAFF)
    assert (await http.get("/_test/admin-only", headers=_as(admin))).json() == {"role": "admin"}
    assert (await http.get("/_test/either", headers=_as(staff))).json() == {"role": "staff"}


async def test_the_wrong_role_is_a_403_security_event(
    http: httpx.AsyncClient, db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    staff = await _staff(db_session, role=StaffRole.STAFF)
    response = await http.get("/_test/admin-only", headers=_as(staff))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    (event,) = _events(capsys.readouterr().err, "permission_denied")
    assert event["role"] == "staff"
    assert event["required_roles"] == ["admin"]
    assert event["staff_id"] == str(staff.id)


def test_require_role_needs_at_least_one_role() -> None:
    with pytest.raises(ValueError, match="role"):
        require_role()


async def test_the_staff_id_is_on_the_request_log_lines(
    http: httpx.AsyncClient, db_session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    staff = await _staff(db_session)
    await http.get("/_test/whoami", headers=_as(staff))
    (access,) = _events(capsys.readouterr().err, "request")
    assert access["staff_id"] == str(staff.id)


async def test_staff_routes_are_limited_per_staff_member(
    http: httpx.AsyncClient, test_routes: GizeAPI, db_session: AsyncSession
) -> None:
    busy = await _staff(db_session)
    other = await _staff(db_session)
    limiter: RateLimiter = test_routes.state.rate_limiter
    for _ in range(120):  # STAFF allows 120 a minute
        await limiter.check(STAFF, staff_id=str(busy.id), ip="192.0.2.1")
    response = await http.get("/_test/whoami", headers=_as(busy))
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert (await http.get("/_test/whoami", headers=_as(other))).status_code == 200


async def test_users_who_are_not_staff_are_throttled_per_ip(
    http: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Supabase may let anyone sign up: valid tokens of strangers count as failures."""
    headers = bearer(make_token(sub=str(uuid.uuid4())))
    statuses = [(await http.get("/_test/whoami", headers=headers)).status_code for _ in range(31)]
    assert statuses == [403] * 30 + [429]
    staff = await _staff(db_session)  # deactivated staff and valid staff are not counted
    assert (await http.get("/_test/whoami", headers=_as(staff))).status_code == 200


async def test_inactive_staff_are_not_counted_as_attacks(
    http: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    headers = _as(await _staff(db_session, is_active=False))
    statuses = {(await http.get("/_test/whoami", headers=headers)).status_code for _ in range(31)}
    assert statuses == {403}


async def test_staff_get_through_while_redis_is_down(
    http: httpx.AsyncClient,
    test_routes: GizeAPI,
    db_session: AsyncSession,
    make_settings: SettingsFactory,
) -> None:
    await test_routes.state.rate_limiter.aclose()
    test_routes.state.rate_limiter = RateLimiter.for_settings(
        make_settings(redis_url=UNREACHABLE_REDIS_URL), telemetry=None
    )
    staff = await _staff(db_session)
    assert (await http.get("/_test/whoami", headers=_as(staff))).status_code == 200


async def test_the_database_down_is_503(
    http: httpx.AsyncClient, test_routes: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    test_routes.dependency_overrides[get_session] = down
    response = await http.get("/_test/whoami", headers=bearer(make_token()))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def _v1_routes(app: GizeAPI) -> Iterator[tuple[str, Dependant]]:
    """Every /v1 API route as served (FastAPI 0.162 keeps included routers unflattened)."""
    for context in iter_route_contexts(app.routes):
        path = context.path or ""
        if isinstance(context.original_route, APIRoute) and path.startswith("/v1/"):
            yield path, context.dependant


def _calls(dependant: Dependant) -> Iterator[object]:
    stack = [dependant]
    while stack:
        current = stack.pop()
        yield current.call
        stack.extend(current.dependencies)


PUBLIC_ROUTES: frozenset[str] = frozenset()  # e.g. registration; each one listed in its PR


def test_every_v1_route_needs_active_staff_and_is_rate_limited(
    make_settings: SettingsFactory,
) -> None:
    """A route someone forgot to guard fails here, not in production."""
    routes = list(_v1_routes(create_app(make_settings())))
    assert "/v1/staff/me" in [path for path, _ in routes]  # the check covers something
    for path, dependant in routes:
        if path in PUBLIC_ROUTES:
            continue
        calls = list(_calls(dependant))
        assert current_staff in calls, path
        assert staff_rate_limit in calls, path


def test_an_unguarded_route_would_be_caught(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())
    unguarded = APIRouter(prefix="/v1/oops")

    @unguarded.get("/open")
    async def open_route() -> None:
        return None

    app.include_router(unguarded)
    calls = {path: list(_calls(dependant)) for path, dependant in _v1_routes(app)}
    assert current_staff not in calls["/v1/oops/open"]
    assert staff_rate_limit not in calls["/v1/oops/open"]


def test_staff_router_adds_the_rate_limit() -> None:
    router = staff_router(prefix="/x")
    assert isinstance(router, APIRouter)
    assert [dependency.dependency for dependency in router.dependencies] == [staff_rate_limit]
