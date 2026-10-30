"""GZ-021: corridors and companies that routes use (hand-offs from GZ-020 and GZ-029).

A corridor a live route runs on can't be deleted or have its areas changed (owner
decision). A company can't be deactivated while one of its routes is active or paused,
nor deleted while it has any live route.
"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.main import create_app
from gize_api.routes.models import RouteStatus
from tests.factories import AreaFactory
from tests.routes.support import add_company, add_corridor, add_route
from tests.staff.support import add_admin, signed_in
from tests.support import SettingsFactory

START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def test_a_corridor_a_route_runs_on_cannot_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    corridor = await add_corridor(db_session)
    await add_route(db_session, corridor_id=corridor)
    response = await api.delete(f"/v1/corridors/{corridor}", headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (409, "CORRIDOR_IN_USE")


async def test_a_corridor_only_deleted_routes_used_can_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    corridor = await add_corridor(db_session)
    await add_route(db_session, corridor_id=corridor, deleted_at=START)
    response = await api.delete(f"/v1/corridors/{corridor}", headers=signed_in(admin_id))
    assert response.status_code == 204


async def test_a_corridors_areas_cannot_change_under_its_routes(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    corridor = await add_corridor(db_session)
    await add_route(db_session, corridor_id=corridor)
    async with db_session.begin():
        elsewhere = (await AreaFactory().create(db_session)).id
    path = f"/v1/corridors/{corridor}"
    change = await api.patch(
        path, json={"destination_area_id": str(elsewhere)}, headers=signed_in(admin_id)
    )
    assert (change.status_code, change.json()["code"]) == (409, "CORRIDOR_IN_USE")
    current = (await api.get(path, headers=signed_in(admin_id))).json()
    same = {"destination_area_id": current["destination_area"]["id"]}
    assert (await api.patch(path, json=same, headers=signed_in(admin_id))).status_code == 200


@pytest.mark.parametrize(
    ("status", "allowed"),
    [
        (RouteStatus.DRAFT, True),
        (RouteStatus.ACTIVE, False),
        (RouteStatus.PAUSED, False),
    ],
)
async def test_a_company_with_a_running_route_cannot_be_deactivated(
    api: httpx.AsyncClient, db_session: AsyncSession, status: RouteStatus, allowed: bool
) -> None:
    admin_id = (await add_admin(db_session)).id
    company = await add_company(db_session)
    await add_route(
        db_session, route_type="corporate", company_id=company, launch_minimum=None, status=status
    )
    response = await api.patch(
        f"/v1/companies/{company}", json={"is_active": False}, headers=signed_in(admin_id)
    )
    if allowed:
        assert response.status_code == 200
    else:
        assert (response.status_code, response.json()["code"]) == (409, "COMPANY_IN_USE")


async def test_a_company_with_any_route_cannot_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await add_admin(db_session)).id
    company = await add_company(db_session)
    await add_route(db_session, route_type="corporate", company_id=company, launch_minimum=None)
    response = await api.delete(f"/v1/companies/{company}", headers=signed_in(admin_id))
    assert (response.status_code, response.json()["code"]) == (409, "COMPANY_IN_USE")
    gone = await add_company(db_session)
    await add_route(
        db_session, route_type="corporate", company_id=gone, launch_minimum=None, deleted_at=START
    )
    assert (
        await api.delete(f"/v1/companies/{gone}", headers=signed_in(admin_id))
    ).status_code == 204


def test_in_use_codes_are_in_the_contract(make_settings: SettingsFactory) -> None:
    paths = create_app(make_settings()).openapi()["paths"]
    corridor, company = paths["/v1/corridors/{corridor_id}"], paths["/v1/companies/{company_id}"]
    assert "CORRIDOR_IN_USE" in corridor["delete"]["responses"]["409"]["x-error-codes"]
    assert "CORRIDOR_IN_USE" in corridor["patch"]["responses"]["409"]["x-error-codes"]
    assert "COMPANY_IN_USE" in company["delete"]["responses"]["409"]["x-error-codes"]
    assert "COMPANY_IN_USE" in company["patch"]["responses"]["409"]["x-error-codes"]
