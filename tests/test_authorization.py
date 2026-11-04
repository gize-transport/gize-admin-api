"""GZ-060: who may call what, endpoint by endpoint, before anything faces the internet.

`ACCESS` names every endpoint the app serves and who may call it, from the permissions in
docs/product-brief.md ("Roles and permissions"):

- admins only: prices, every delete, staff accounts and roles, the audit log;
- active staff (staff or admin): everything else;
- anyone: the health checks and the public app's phone verification, catalogue and
  sign-ups.

Each endpoint is then called for real: with no token (401), as someone who isn't staff or
is deactivated (403), as staff and as an admin. A new endpoint fails the first test until
it is added here, so its access is a decision someone wrote down, not a default.
(tests/staff/test_auth.py checks the same guards by inspecting the dependencies.)
"""

import uuid
from typing import Literal

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.main import create_app
from gize_api.staff.models import StaffRole
from tests.staff.support import add_admin, add_staff, with_key
from tests.staff.test_auth import UNGUARDED_ROUTES
from tests.support import SettingsFactory

Access = Literal["admin", "staff", "public"]
ADMIN: Access = "admin"
STAFF: Access = "staff"
PUBLIC: Access = "public"

ACCESS: dict[tuple[str, str], Access] = {
    ("GET", "/health/live"): PUBLIC,
    ("GET", "/health/ready"): PUBLIC,
    # The public app (GZ-031, GZ-036): rate limited per caller instead of signed in.
    ("POST", "/v1/verification/request"): PUBLIC,
    ("POST", "/v1/verification/verify"): PUBLIC,
    ("GET", "/v1/public/areas"): PUBLIC,
    ("GET", "/v1/public/routes"): PUBLIC,
    ("POST", "/v1/registrations/adult"): PUBLIC,
    # Staff accounts: admins, except who you are.
    ("GET", "/v1/staff/me"): STAFF,
    ("GET", "/v1/staff"): ADMIN,
    ("POST", "/v1/staff"): ADMIN,
    ("GET", "/v1/staff/{staff_id}"): ADMIN,
    ("PATCH", "/v1/staff/{staff_id}"): ADMIN,
    ("GET", "/v1/audit-log"): ADMIN,
    # Areas, corridors, companies: staff manage them; admins delete.
    # Riders (GZ-034): staff manage them; admins delete.
    ("GET", "/v1/riders"): STAFF,
    ("POST", "/v1/riders"): STAFF,
    ("POST", "/v1/riders/lookup"): STAFF,
    ("GET", "/v1/riders/{rider_id}"): STAFF,
    ("PATCH", "/v1/riders/{rider_id}"): STAFF,
    ("DELETE", "/v1/riders/{rider_id}"): ADMIN,
    ("GET", "/v1/areas"): STAFF,
    ("POST", "/v1/areas"): STAFF,
    ("GET", "/v1/areas/{area_id}"): STAFF,
    ("PATCH", "/v1/areas/{area_id}"): STAFF,
    ("DELETE", "/v1/areas/{area_id}"): ADMIN,
    ("GET", "/v1/corridors"): STAFF,
    ("POST", "/v1/corridors"): STAFF,
    ("GET", "/v1/corridors/{corridor_id}"): STAFF,
    ("PATCH", "/v1/corridors/{corridor_id}"): STAFF,
    ("DELETE", "/v1/corridors/{corridor_id}"): ADMIN,
    ("GET", "/v1/companies"): STAFF,
    ("POST", "/v1/companies"): STAFF,
    ("GET", "/v1/companies/{company_id}"): STAFF,
    ("PATCH", "/v1/companies/{company_id}"): STAFF,  # deactivating one too
    ("DELETE", "/v1/companies/{company_id}"): ADMIN,
    # Routes: staff edit them and move their statuses; admins set prices and delete.
    ("GET", "/v1/routes"): STAFF,
    ("POST", "/v1/routes"): STAFF,
    ("GET", "/v1/routes/{route_id}"): STAFF,
    ("PATCH", "/v1/routes/{route_id}"): STAFF,
    ("PUT", "/v1/routes/{route_id}/areas"): STAFF,
    ("PUT", "/v1/routes/{route_id}/status"): STAFF,
    ("POST", "/v1/routes/{route_id}/copies"): STAFF,
    ("GET", "/v1/routes/{route_id}/launch-months"): STAFF,
    ("PUT", "/v1/routes/{route_id}/prices"): ADMIN,
    ("DELETE", "/v1/routes/{route_id}"): ADMIN,
    # Route requests: staff record and settle them; admins delete.
    ("GET", "/v1/route-requests"): STAFF,
    ("POST", "/v1/route-requests"): STAFF,
    ("GET", "/v1/route-requests/summary"): STAFF,
    ("GET", "/v1/route-requests/{request_id}"): STAFF,
    ("PATCH", "/v1/route-requests/{request_id}"): STAFF,
    ("PUT", "/v1/route-requests/{request_id}/status"): STAFF,
    ("DELETE", "/v1/route-requests/{request_id}"): ADMIN,
}


def _served(settings_factory: SettingsFactory) -> set[tuple[str, str]]:
    """Every method and path the app serves outside local and staging (no docs). HEAD
    comes with GET; a route that isn't an API route (a mount, a websocket) is listed as
    `*` and so can't match the matrix."""
    app = create_app(settings_factory(app_env="production"))
    return {
        (method, context.path or repr(context.original_route))
        for context in iter_route_contexts(app.routes)
        for method in _methods(context.methods)
    }


def _methods(methods: set[str] | None) -> set[str]:
    """HEAD is listed only where it comes without a GET."""
    if not methods:
        return {"*"}
    return methods - {"HEAD"} if "GET" in methods else methods


def test_every_endpoint_has_its_access_written_down(make_settings: SettingsFactory) -> None:
    assert _served(make_settings) == set(ACCESS)


def test_a_head_only_route_would_be_listed() -> None:
    assert _methods({"HEAD"}) == {"HEAD"}
    assert _methods({"GET", "HEAD"}) == {"GET"}


def test_public_endpoints_are_the_ones_the_guard_test_allows() -> None:
    """One list of public endpoints, kept in step with tests/staff/test_auth.py."""
    public = {endpoint for endpoint, access in ACCESS.items() if access == PUBLIC}
    assert public <= UNGUARDED_ROUTES


def _concrete(path: str) -> str:
    """The path with an ID in each placeholder (no such row: access comes first)."""
    return path.format_map(_AnyId())


class _AnyId(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return str(uuid.UUID(int=1))


@pytest.mark.parametrize(("method", "path"), sorted(ACCESS), ids=" ".join)
async def test_each_endpoint_lets_in_only_whom_it_should(
    api: httpx.AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    access = ACCESS[(method, path)]
    url = _concrete(path)

    async def status(caller: uuid.UUID | None) -> tuple[int, str | None]:
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        if caller is not None:
            headers = with_key(caller)
        response = await api.request(method, url, json={}, headers=headers)
        code = response.json().get("code") if response.content else None
        return response.status_code, code

    admin = (await add_admin(db_session)).id
    member = (await add_staff(db_session, role=StaffRole.STAFF)).id
    gone = (await add_admin(db_session, is_active=False)).id

    if access == PUBLIC:
        assert (await status(None))[0] not in (401, 403)
        return
    assert await status(None) == (401, "UNAUTHENTICATED")
    for outsider in (uuid.uuid4(), gone):  # signed in, but not (or no longer) staff
        assert await status(outsider) == (403, "FORBIDDEN")
    member_status = await status(member)
    if access == ADMIN:
        assert member_status == (403, "FORBIDDEN")
    else:
        assert _let_in(method, path, member_status[0]), member_status
    assert _let_in(method, path, (await status(admin))[0])


# Let in, then failing for a reason of this test app's, not of access: no Supabase admin
# client, so an invite can't be sent.
UNAVAILABLE_HERE = {("POST", "/v1/staff")}


def _let_in(method: str, path: str, status: int) -> bool:
    """Past the access checks: an answer, or the endpoint's own refusal (a 404 for the
    made-up ID, a 422 for the empty body), never a 401, a 403 or a server error."""
    if (method, path) in UNAVAILABLE_HERE:
        return status == 503
    return status not in (401, 403) and status < 500
