"""GZ-013: POST /v1/staff, an admin invites a staff member through Supabase.

Supabase is the fake in conftest.py (the real client on a mock transport); the database is
the real test database. Parallel requests are in test_concurrency.py.
"""

import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.staff import service
from gize_api.staff.models import Staff, StaffRole
from gize_api.staff.schemas import EMAIL_PATTERN
from tests.staff.support import (
    STAFF,
    add_admin,
    add_staff,
    audit_entries,
    signed_in,
    with_key,
)
from tests.supabase import FakeSupabase

ALMAZ = {"email": "almaz@example.com", "full_name": "Almaz Tesfaye", "role": "staff"}


async def _staff_with_email(session: AsyncSession, email: str) -> Staff | None:
    async with session.begin():
        return await session.scalar(select(Staff).where(Staff.email == email))


async def test_an_admin_invites_a_staff_member(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session)
    body = {"email": "  Almaz.T@Example.COM ", "full_name": "  Almaz Tesfaye ", "role": "staff"}
    response = await api.post(STAFF, json=body, headers=with_key(admin.id))
    assert response.status_code == 201
    created = response.json()
    user_id = supabase.user_id("almaz.t@example.com")
    assert created == {
        "id": str(user_id),  # the Supabase user ID: sign-in proves who they are
        "full_name": "Almaz Tesfaye",
        "email": "almaz.t@example.com",  # stored trimmed and lower-case
        "role": "staff",
        "is_active": True,
        "first_signed_in_at": None,  # until they accept and sign in
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }
    assert response.headers["Location"] == f"/v1/staff/{user_id}"
    assert response.headers["Cache-Control"] == "no-store"
    assert supabase.invites == [
        {"email": "almaz.t@example.com", "data": {"full_name": "Almaz Tesfaye"}}
    ]
    staff = await _staff_with_email(db_session, "almaz.t@example.com")
    assert staff is not None
    assert (staff.id, staff.role, staff.is_active) == (user_id, StaffRole.STAFF, True)


async def test_the_invite_is_audited(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session)
    response = await api.post(
        STAFF, json=ALMAZ, headers={**with_key(admin.id), "X-Request-ID": "req-invite-1"}
    )
    (entry,) = await audit_entries(db_session, uuid.UUID(response.json()["id"]))
    assert entry.action == "staff.invited"
    assert entry.actor_staff_id == admin.id
    assert entry.before is None
    # Role and status by value; the name and email only as "set".
    assert entry.after == {
        "full_name": REDACTED,
        "email": REDACTED,
        "role": "staff",
        "is_active": True,
    }
    assert entry.request_id == "req-invite-1"


@pytest.mark.parametrize(("role", "alert"), [("staff", False), ("admin", True)])
async def test_an_invite_is_a_security_event_and_a_new_admin_an_alert(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    supabase: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    alert: bool,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def capture(event: str, **fields: Any) -> None:  # noqa: ANN401
        events.append((event, fields))

    monkeypatch.setattr(service, "log_security_event", capture)
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json={**ALMAZ, "role": role}, headers=with_key(admin.id))
    assert response.status_code == 201
    assert events == [
        (
            "staff_invited",
            {
                "sentry": alert,
                "actor_staff_id": str(admin.id),
                "staff_id": response.json()["id"],
                "role": role,
            },
        )
    ]  # IDs and the role only: never the name or email


async def test_a_retry_replays_the_response_without_inviting_again(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session)
    headers = with_key(admin.id)
    first = await api.post(STAFF, json=ALMAZ, headers=headers)
    again = await api.post(STAFF, json=ALMAZ, headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Location"] == first.headers["Location"]
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(supabase.invites) == 1  # no second email
    assert len(await audit_entries(db_session, uuid.UUID(first.json()["id"]))) == 1


async def test_the_same_key_with_another_body_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session)
    headers = with_key(admin.id)
    assert (await api.post(STAFF, json=ALMAZ, headers=headers)).status_code == 201
    other = {**ALMAZ, "email": "someone@example.com"}
    response = await api.post(STAFF, json=other, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_MISMATCH"
    assert len(supabase.invites) == 1


async def test_a_missing_or_malformed_key_is_428(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session)
    for headers in (signed_in(admin.id), {**signed_in(admin.id), "Idempotency-Key": "abc"}):
        response = await api.post(STAFF, json=ALMAZ, headers=headers)
        assert response.status_code == 428
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert supabase.invites == []


@pytest.mark.parametrize("active", [True, False])
async def test_an_email_already_on_staff_is_409_without_inviting(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase, active: bool
) -> None:
    """A deactivated member keeps their email: reactivate them instead."""
    admin = await add_admin(db_session)
    await add_staff(db_session, email="almaz@example.com", is_active=active)
    body = {**ALMAZ, "email": "ALMAZ@example.com "}
    response = await api.post(STAFF, json=body, headers=with_key(admin.id))
    assert response.status_code == 409
    assert response.json()["code"] == "STAFF_EMAIL_TAKEN"
    assert supabase.invites == []


async def test_an_email_with_a_confirmed_supabase_account_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    """Someone who already signs in to Supabase is never turned into staff by an invite:
    an operator adds them deliberately (owner decision, GZ-013)."""
    supabase.answer = lambda request: httpx.Response(
        422, json={"code": 422, "error_code": "email_exists", "msg": "already registered"}
    )
    admin = await add_admin(db_session)
    headers = with_key(admin.id)
    response = await api.post(STAFF, json=ALMAZ, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "AUTH_ACCOUNT_EXISTS"
    assert await _staff_with_email(db_session, "almaz@example.com") is None
    # Nothing was stored for the key: the same request asks Supabase again.
    await api.post(STAFF, json=ALMAZ, headers=headers)
    assert len(supabase.invites) == 2


async def test_a_supabase_user_who_is_already_staff_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    """Supabase's user for this email is on staff under another email (changed since)."""
    admin = await add_admin(db_session)
    member = await add_staff(db_session, email="old.address@example.com")
    supabase.ids["almaz@example.com"] = member.id
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 409
    assert response.json()["code"] == "AUTH_ACCOUNT_EXISTS"
    assert await _staff_with_email(db_session, "almaz@example.com") is None


async def test_an_email_supabase_rejects_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    supabase.answer = lambda request: httpx.Response(
        400, json={"code": 400, "error_code": "email_address_invalid"}
    )
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "VALIDATION_FAILED"
    assert [(error["location"], error["field"]) for error in problem["errors"]] == [
        ("body", "/email")
    ]


EMAIL_LIMIT = {"code": 429, "error_code": "over_email_send_rate_limit"}


@pytest.mark.parametrize(
    ("reply", "retry_after"),
    [
        (httpx.Response(500), "30"),
        (httpx.Response(200, content=b"<html>"), "30"),
        # Supabase's hourly email limit: its own Retry-After, within an hour.
        (httpx.Response(429, json=EMAIL_LIMIT), "60"),
        (httpx.Response(429, json=EMAIL_LIMIT, headers={"Retry-After": "120"}), "120"),
        (httpx.Response(429, json=EMAIL_LIMIT, headers={"Retry-After": "99999"}), "3600"),
        # Only a configuration fix helps: no point in retrying soon.
        (httpx.Response(401, json={"msg": "Invalid API key"}), None),
        (
            httpx.Response(400, json={"code": 400, "error_code": "email_address_not_authorized"}),
            None,
        ),
    ],
)
async def test_supabase_failing_is_503_and_changes_nothing(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    supabase: FakeSupabase,
    reply: httpx.Response,
    retry_after: str | None,
) -> None:
    supabase.answer = lambda request: reply
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers.get("Retry-After") == retry_after
    assert "supabase" not in response.text.lower()  # no internals in the response
    assert await _staff_with_email(db_session, "almaz@example.com") is None


async def test_supabase_unreachable_is_503(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    supabase.answer = unreachable
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 503


async def test_without_the_secret_key_invites_are_503(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """SUPABASE_SECRET_KEY is optional: everything else works without it."""
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize(
    "body",
    [
        {**ALMAZ, "surprise": 1},
        {"full_name": "Almaz", "role": "staff"},
        {"email": "almaz@example.com", "role": "staff"},
        {"email": "almaz@example.com", "full_name": "Almaz"},
        {**ALMAZ, "email": "almaz"},
        {**ALMAZ, "email": "@example.com"},
        {**ALMAZ, "email": "almaz@"},
        {**ALMAZ, "email": "almaz@localhost"},
        {**ALMAZ, "email": "al maz@example.com"},
        {**ALMAZ, "email": "al@maz@example.com"},
        {**ALMAZ, "email": "almaz@exam\u0000ple.com"},
        {**ALMAZ, "email": "al\u200bmaz@example.com"},  # invisible
        {**ALMAZ, "email": "almaz@ex\u202eample.com"},  # right-to-left override
        {**ALMAZ, "email": "almaz@-example.com"},
        {**ALMAZ, "email": "almaz@example..com"},
        {**ALMAZ, "email": "a" * 65 + "@example.com"},
        {**ALMAZ, "email": "a@" + "b" * 316 + ".com"},
        {**ALMAZ, "email": 7},
        {**ALMAZ, "full_name": ""},
        {**ALMAZ, "full_name": "   "},
        {**ALMAZ, "full_name": "x" * 201},
        {**ALMAZ, "full_name": "Almaz\u0007"},
        {**ALMAZ, "role": "owner"},
        {**ALMAZ, "role": None},
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase, body: object
) -> None:
    admin = await add_admin(db_session)
    response = await api.post(STAFF, json=body, headers=with_key(admin.id))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert supabase.invites == []


async def test_staff_who_are_not_admins_get_403(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    member = await add_staff(db_session)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(member.id))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert supabase.invites == []


async def test_a_deactivated_admin_gets_403(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    admin = await add_admin(db_session, is_active=False)
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 403
    assert supabase.invites == []


@pytest.mark.parametrize("body", [{"surprise": 1}, {**ALMAZ, "email": "nope"}])
async def test_the_role_is_checked_before_the_body_fields_and_the_key(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase, body: object
) -> None:
    """Field values and the key are judged after the role. (A body that is not JSON at all
    is a 422 before anything else: FastAPI decodes it before any dependency runs.)"""
    member = await add_staff(db_session)
    assert (await api.post(STAFF, json=body, headers=signed_in(member.id))).status_code == 403
    assert (await api.post(STAFF, json=body)).status_code == 401


async def test_no_token_is_401(api: httpx.AsyncClient, supabase: FakeSupabase) -> None:
    response = await api.post(STAFF, json=ALMAZ, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401
    assert supabase.invites == []


async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    supabase: FakeSupabase,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    admin_id = uuid.uuid4()
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin_id))
    assert response.status_code == 503
    assert supabase.invites == []


async def test_invites_have_their_own_hourly_limit(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    """Each invite sends an email: 20 an hour per admin, on top of the STAFF limit."""
    admin = await add_admin(db_session)
    for number in range(20):
        body = {**ALMAZ, "email": f"person{number}@example.com"}
        assert (await api.post(STAFF, json=body, headers=with_key(admin.id))).status_code == 201
    response = await api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMITED"
    assert response.headers["Retry-After"]
    assert len(supabase.invites) == 20


async def test_refusals_and_replays_do_not_use_up_the_invite_limit(
    api: httpx.AsyncClient, db_session: AsyncSession, supabase: FakeSupabase
) -> None:
    """Only requests about to send an email count: a client retrying a refused or finished
    request cannot lock its admin out of inviting for an hour."""
    admin = await add_admin(db_session)
    await add_staff(db_session, email="taken@example.com")
    finished = with_key(admin.id)
    assert (await api.post(STAFF, json=ALMAZ, headers=finished)).status_code == 201
    for _ in range(10):
        assert (await api.post(STAFF, json=ALMAZ, headers=signed_in(admin.id))).status_code == 428
        taken = {**ALMAZ, "email": "taken@example.com"}
        assert (await api.post(STAFF, json=taken, headers=with_key(admin.id))).status_code == 409
        assert (await api.post(STAFF, json=ALMAZ, headers=finished)).status_code == 201  # replay
    for number in range(19):  # 20 emails in all
        body = {**ALMAZ, "email": f"person{number}@example.com"}
        assert (await api.post(STAFF, json=body, headers=with_key(admin.id))).status_code == 201
    assert len(supabase.invites) == 20


def test_the_email_pattern_is_valid_in_browsers() -> None:
    """The contract's pattern is JSON Schema (ECMA regex): the portal may use it in an
    `<input pattern>`, which browsers compile with the `v` flag, the strictest."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is needed to check the pattern as browsers compile it")
    script = (
        "const p = process.argv[1];"
        "for (const f of ['', 'u', 'v']) {"
        "  const r = new RegExp(p, f);"
        "  if (!r.test('almaz.t+gize@sub-1.example.et') || r.test('a b@example.com'))"
        "    throw new Error(f);"
        "}"
    )
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [node, "-e", script, EMAIL_PATTERN], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
