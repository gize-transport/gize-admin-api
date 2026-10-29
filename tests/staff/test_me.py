"""GZ-011: GET /v1/staff/me, the admin portal's first call after sign-in."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI, create_app
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.support import SettingsFactory
from tests.tokens import bearer, make_token

ME = "/v1/staff/me"


async def test_me_returns_who_i_am(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    async with db_session.begin():
        staff = await StaffFactory().create(
            db_session, full_name="Abebe Kebede", email="abebe@example.com", role=StaffRole.ADMIN
        )
    response = await api.get(ME, headers=bearer(make_token(sub=str(staff.id))))
    assert response.status_code == 200
    assert response.json() == {
        "id": str(staff.id),
        "full_name": "Abebe Kebede",
        "email": "abebe@example.com",
        "role": "admin",
    }
    assert response.headers["Cache-Control"] == "no-store"  # personal data


async def test_me_without_a_token_is_401(api: httpx.AsyncClient) -> None:
    response = await api.get(ME)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer not.a.token", "Bearer "])
async def test_me_with_a_malformed_authorization_header_is_401(
    api: httpx.AsyncClient, header: str
) -> None:
    assert (await api.get(ME, headers={"Authorization": header})).status_code == 401


async def test_me_for_someone_who_is_not_staff_is_403(api: httpx.AsyncClient) -> None:
    response = await api.get(ME, headers=bearer(make_token(sub=str(uuid.uuid4()))))
    assert response.status_code == 403


async def test_me_for_deactivated_staff_is_403(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with db_session.begin():
        staff = await StaffFactory().create(db_session, is_active=False)
    response = await api.get(ME, headers=bearer(make_token(sub=str(staff.id))))
    assert response.status_code == 403
    assert "full_name" not in response.text


async def test_me_shows_the_role_from_our_table(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with db_session.begin():
        staff = await StaffFactory().create(db_session, role=StaffRole.STAFF)
    token = make_token(sub=str(staff.id), app_metadata={"role": "admin"})
    assert (await api.get(ME, headers=bearer(token))).json()["role"] == "staff"


async def test_me_with_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(ME, headers=bearer(make_token()))
    assert response.status_code == 503


def test_me_is_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    operation = schema["paths"][ME]["get"]
    assert operation["operationId"] == "staff_me"
    assert operation["security"] == [{"HTTPBearer": []}]
    assert {"200", "401", "403", "429", "503"} <= set(operation["responses"])
    me = schema["components"]["schemas"]["StaffMe"]
    assert set(me["properties"]) == {"id", "full_name", "email", "role"}
    assert me["additionalProperties"] is False


async def test_the_first_request_records_the_first_sign_in(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """GZ-013: an invited member who has signed in has accepted their invitation."""
    async with db_session.begin():
        staff = await StaffFactory().create(db_session)
        staff_id = staff.id
        assert staff.first_signed_in_at is None
    assert (await api.get(ME, headers=bearer(make_token(sub=str(staff_id))))).status_code == 200
    async with db_session.begin():
        assert (await db_session.get_one(Staff, staff_id)).first_signed_in_at is not None


async def test_the_first_sign_in_is_kept(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    first = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    async with db_session.begin():
        staff_id = (await StaffFactory().create(db_session, first_signed_in_at=first)).id
    assert (await api.get(ME, headers=bearer(make_token(sub=str(staff_id))))).status_code == 200
    async with db_session.begin():
        assert (await db_session.get_one(Staff, staff_id)).first_signed_in_at == first


async def test_a_refused_request_records_no_sign_in(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with db_session.begin():
        staff_id = (await StaffFactory().create(db_session, is_active=False)).id
    assert (await api.get(ME, headers=bearer(make_token(sub=str(staff_id))))).status_code == 403
    async with db_session.begin():
        assert (await db_session.get_one(Staff, staff_id)).first_signed_in_at is None
