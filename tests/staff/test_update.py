"""GZ-013: PATCH /v1/staff/{staff_id}, an admin changes a role or deactivates someone.

There is always at least one active admin: no change may remove the last one. Parallel
requests are in test_concurrency.py.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.staff import service
from gize_api.staff.models import Staff, StaffRole
from gize_api.staff.repository import get_staff
from tests.staff.support import STAFF, add_admin, add_staff, audit_entries, signed_in


def _member(staff: Staff | uuid.UUID) -> str:
    return f"{STAFF}/{staff.id if isinstance(staff, Staff) else staff}"


async def _reread(session: AsyncSession, staff_id: uuid.UUID) -> Staff:
    async with session.begin():
        staff = await get_staff(session, staff_id)
    assert staff is not None
    return staff


async def _changes(session: AsyncSession, staff_id: uuid.UUID) -> list[tuple[str, Any, Any]]:
    return [
        (entry.action, entry.before, entry.after)
        for entry in await audit_entries(session, staff_id)
    ]


@pytest.fixture
def security_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(event: str, **fields: Any) -> None:  # noqa: ANN401
        events.append((event, fields))

    monkeypatch.setattr(service, "log_security_event", capture)
    return events


async def test_an_admin_promotes_a_member(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    security_events: list[tuple[str, dict[str, Any]]],
) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session)
    response = await api.patch(_member(member), json={"role": "admin"}, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["role"] == "admin"
    assert (await _reread(db_session, member.id)).role is StaffRole.ADMIN
    assert await _changes(db_session, member.id) == [
        ("staff.role_changed", {"role": "staff"}, {"role": "admin"})
    ]
    (entry,) = await audit_entries(db_session, member.id)
    assert entry.actor_staff_id == admin.id
    # A new admin is an alert; IDs and roles only.
    assert security_events == [
        (
            "staff_role_changed",
            {
                "sentry": True,
                "actor_staff_id": str(admin.id),
                "staff_id": str(member.id),
                "role_before": "staff",
                "role": "admin",
            },
        )
    ]


async def test_an_admin_demotes_another_admin(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    security_events: list[tuple[str, dict[str, Any]]],
) -> None:
    admin = await add_admin(db_session)
    other = await add_admin(db_session)
    response = await api.patch(_member(other), json={"role": "staff"}, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert await _changes(db_session, other.id) == [
        ("staff.role_changed", {"role": "admin"}, {"role": "staff"})
    ]
    assert [(event, fields["sentry"]) for event, fields in security_events] == [
        ("staff_role_changed", False)
    ]


async def test_a_deactivated_member_is_locked_out(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    security_events: list[tuple[str, dict[str, Any]]],
) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session)
    assert (await api.get(f"{STAFF}/me", headers=signed_in(member.id))).status_code == 200
    response = await api.patch(
        _member(member), json={"is_active": False}, headers=signed_in(admin.id)
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert (await api.get(f"{STAFF}/me", headers=signed_in(member.id))).status_code == 403
    assert await _changes(db_session, member.id) == [
        ("staff.deactivated", {"is_active": True}, {"is_active": False})
    ]
    assert [event for event, _ in security_events] == ["staff_deactivated"]


async def test_a_deactivated_member_can_be_reactivated(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    security_events: list[tuple[str, dict[str, Any]]],
) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session, is_active=False)
    response = await api.patch(
        _member(member), json={"is_active": True}, headers=signed_in(admin.id)
    )
    assert response.status_code == 200
    assert (await api.get(f"{STAFF}/me", headers=signed_in(member.id))).status_code == 200
    assert await _changes(db_session, member.id) == [
        ("staff.reactivated", {"is_active": False}, {"is_active": True})
    ]
    assert [event for event, _ in security_events] == ["staff_reactivated"]


async def test_role_and_status_in_one_request_are_two_entries(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session, is_active=False)
    body = {"role": "admin", "is_active": True}
    response = await api.patch(_member(member), json=body, headers=signed_in(admin.id))
    assert response.status_code == 200
    assert sorted(await _changes(db_session, member.id)) == [
        ("staff.reactivated", {"is_active": False}, {"is_active": True}),
        ("staff.role_changed", {"role": "staff"}, {"role": "admin"}),
    ]


async def test_a_change_to_what_is_already_there_records_nothing(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    security_events: list[tuple[str, dict[str, Any]]],
) -> None:
    """So a retried PATCH is harmless: no Idempotency-Key needed."""
    admin = await add_admin(db_session)
    member = await add_staff(db_session)
    body = {"role": "staff", "is_active": True}
    for _ in range(2):
        response = await api.patch(_member(member), json=body, headers=signed_in(admin.id))
        assert response.status_code == 200
    assert await _changes(db_session, member.id) == []
    assert security_events == []


@pytest.mark.parametrize("body", [{"role": "staff"}, {"is_active": False}])
async def test_the_last_active_admin_cannot_step_down(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, object]
) -> None:
    admin_id = (await add_admin(db_session)).id  # the refused request's rollback expires rows
    await add_admin(db_session, is_active=False)  # a deactivated admin does not count
    response = await api.patch(_member(admin_id), json=body, headers=signed_in(admin_id))
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ACTIVE_ADMIN"
    staff = await _reread(db_session, admin_id)
    assert (staff.role, staff.is_active) == (StaffRole.ADMIN, True)
    assert await _changes(db_session, admin_id) == []


@pytest.mark.parametrize("body", [{"role": "staff"}, {"is_active": False}])
async def test_with_another_active_admin_either_may_step_down(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, object]
) -> None:
    admin = await add_admin(db_session)
    other = await add_admin(db_session)
    response = await api.patch(_member(admin), json=body, headers=signed_in(admin.id))
    assert response.status_code == 200
    # Now `other` is the last active admin.
    response = await api.patch(_member(other), json=body, headers=signed_in(other.id))
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ACTIVE_ADMIN"


async def test_an_admin_who_has_never_signed_in_does_not_count(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """An invited admin may never accept (a mistyped email): stepping down in their favour
    would leave nobody able to sign in as admin, and the first-admin command refuses
    once any admin exists. They count once they have signed in."""
    admin_id = (await add_admin(db_session)).id
    invited_id = (await add_admin(db_session, first_signed_in_at=None)).id
    body = {"role": "staff"}
    response = await api.patch(_member(admin_id), json=body, headers=signed_in(admin_id))
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ACTIVE_ADMIN"

    assert (await api.get(f"{STAFF}/me", headers=signed_in(invited_id))).status_code == 200
    response = await api.patch(_member(admin_id), json=body, headers=signed_in(admin_id))
    assert response.status_code == 200


async def test_an_admin_may_deactivate_another_admin(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    other = await add_admin(db_session)
    body = {"is_active": False}
    response = await api.patch(_member(other), json=body, headers=signed_in(admin.id))
    assert response.status_code == 200


async def test_an_unknown_member_is_404(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    body = {"role": "admin"}
    response = await api.patch(_member(uuid.uuid4()), json=body, headers=signed_in(admin.id))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": None},
        {"is_active": None},
        {"role": "owner"},
        {"is_active": "yes"},
        {"is_active": 0},
        {"role": "staff", "surprise": 1},
        {"full_name": "New Name"},  # names and emails are not changed here
        {"email": "new@example.com"},
        [],
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    admin = await add_admin(db_session)
    member = await add_staff(db_session)
    response = await api.patch(_member(member), json=body, headers=signed_in(admin.id))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert await _changes(db_session, member.id) == []


async def test_a_malformed_member_id_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_admin(db_session)
    response = await api.patch(
        f"{STAFF}/not-a-uuid", json={"role": "admin"}, headers=signed_in(admin.id)
    )
    assert response.status_code == 422


async def test_staff_who_are_not_admins_get_403(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    response = await api.patch(
        _member(member), json={"role": "admin"}, headers=signed_in(member.id)
    )
    assert response.status_code == 403  # nobody promotes themselves
    assert (await _reread(db_session, member.id)).role is StaffRole.STAFF


async def test_a_deactivated_admin_gets_403(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await add_admin(db_session)
    admin = await add_admin(db_session, is_active=False)
    response = await api.patch(
        _member(admin), json={"is_active": True}, headers=signed_in(admin.id)
    )
    assert response.status_code == 403  # nobody reactivates themselves


async def test_a_bad_value_is_one_error_on_its_field(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The portal shows each error next to its input: the pointer must be the field's."""
    admin = await add_admin(db_session)
    member = await add_staff(db_session)
    for field, value in (("is_active", "true"), ("role", "ADMIN")):
        response = await api.patch(
            _member(member), json={field: value}, headers=signed_in(admin.id)
        )
        errors = response.json()["errors"]
        assert [(error["location"], error["field"]) for error in errors] == [("body", f"/{field}")]


@pytest.mark.parametrize("body", [{}, {"surprise": 1}, {"role": "owner"}])
async def test_the_role_is_checked_before_the_body(
    api: httpx.AsyncClient, db_session: AsyncSession, body: object
) -> None:
    member = await add_staff(db_session)
    assert (
        await api.patch(_member(member), json=body, headers=signed_in(member.id))
    ).status_code == 403
    assert (await api.patch(_member(member), json=body)).status_code == 401


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.patch(
        _member(uuid.uuid4()), json={"role": "admin"}, headers=signed_in(uuid.uuid4())
    )
    assert response.status_code == 503
