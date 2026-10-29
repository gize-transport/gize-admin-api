"""GZ-014: the command that makes an existing Supabase user the first admin.

The database part runs on real, committed rows (two runs at once must not both succeed);
Supabase is a fake, or the real client on a mock transport.
"""

import asyncio
import uuid
from dataclasses import replace

import asyncpg
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.cli import first_admin
from gize_api.cli.first_admin import FIRST_ADMIN_LOCK_ID, FirstAdminRefusedError, create_first_admin
from gize_api.core.config import ConfigError
from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import AuthAdminError, AuthUser, SupabaseAuthAdmin
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.integrations.test_supabase_auth import SECRET_KEY, user_json
from tests.support import (
    UNREACHABLE_DATABASE_URL,
    DatabaseUrls,
    SettingsFactory,
    public_resolver,
)

USER = AuthUser(
    id=uuid.UUID(int=42),
    email="abebe@example.com",
    email_confirmed=True,
    banned=False,
    deleted=False,
    anonymous=False,
)


class FakeAuth:
    """Supabase's admin API, answering from a dict of users."""

    def __init__(self, *users: AuthUser, error: AuthAdminError | None = None) -> None:
        self.users = {user.id: user for user in users}
        self.error = error
        self.calls = 0

    async def get_user(self, user_id: uuid.UUID) -> AuthUser | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.users.get(user_id)


async def _staff(sessions: async_sessionmaker[AsyncSession]) -> list[Staff]:
    async with sessions() as session, session.begin():
        return list((await session.scalars(select(Staff).order_by(Staff.email))).all())


async def test_a_supabase_user_becomes_the_first_admin(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await create_first_admin(committed_sessionmaker, FakeAuth(USER), USER.id, "Abebe Kebede")
    (staff,) = await _staff(committed_sessionmaker)
    assert (staff.id, staff.full_name, staff.email) == (USER.id, "Abebe Kebede", USER.email)
    assert staff.role is StaffRole.ADMIN
    assert staff.is_active


async def test_an_existing_staff_member_is_promoted(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_sessionmaker() as session, session.begin():
        await StaffFactory().create(
            session, id=USER.id, email="old@example.com", role=StaffRole.STAFF, is_active=False
        )
    await create_first_admin(committed_sessionmaker, FakeAuth(USER), USER.id, "Abebe Kebede")
    (staff,) = await _staff(committed_sessionmaker)
    assert staff.role is StaffRole.ADMIN
    assert staff.is_active
    assert (staff.full_name, staff.email) == ("Abebe Kebede", USER.email)  # Supabase's email


@pytest.mark.parametrize("active", [True, False])
async def test_it_refuses_once_any_admin_exists(
    committed_sessionmaker: async_sessionmaker[AsyncSession], active: bool
) -> None:
    async with committed_sessionmaker() as session, session.begin():
        await StaffFactory().create(session, role=StaffRole.ADMIN, is_active=active)
    with pytest.raises(FirstAdminRefusedError, match="admin already exists"):
        await create_first_admin(committed_sessionmaker, FakeAuth(USER), USER.id, "Abebe")
    assert len(await _staff(committed_sessionmaker)) == 1


@pytest.mark.parametrize(
    ("user", "message"),
    [
        (None, "no Supabase user"),
        (replace(USER, email=None), "no email"),
        (replace(USER, email_confirmed=False), "not confirmed"),
        (replace(USER, banned=True), "banned"),
        (replace(USER, deleted=True), "deleted"),
        (replace(USER, anonymous=True), "anonymous"),
    ],
)
async def test_it_refuses_users_who_cannot_be_admin(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    user: AuthUser | None,
    message: str,
) -> None:
    auth = FakeAuth(*([user] if user else []))
    with pytest.raises(FirstAdminRefusedError, match=message):
        await create_first_admin(committed_sessionmaker, auth, USER.id, "Abebe")
    assert await _staff(committed_sessionmaker) == []


async def test_supabase_down_changes_nothing(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    auth = FakeAuth(USER, error=AuthAdminError("could not reach Supabase"))
    with pytest.raises(AuthAdminError):
        await create_first_admin(committed_sessionmaker, auth, USER.id, "Abebe")
    assert await _staff(committed_sessionmaker) == []


async def test_an_email_taken_by_another_staff_member_is_refused(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_sessionmaker() as session, session.begin():
        await StaffFactory().create(session, email=USER.email)
    with pytest.raises(FirstAdminRefusedError, match="already uses this email"):
        await create_first_admin(committed_sessionmaker, FakeAuth(USER), USER.id, "Abebe")
    (staff,) = await _staff(committed_sessionmaker)
    assert staff.id != USER.id


async def test_concurrent_runs_make_exactly_one_admin(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    users = [
        replace(USER, id=uuid.uuid4(), email=f"admin{number}@example.com") for number in range(5)
    ]
    auth = FakeAuth(*users)
    outcomes = await asyncio.gather(
        *(
            create_first_admin(committed_sessionmaker, auth, user.id, f"Admin {number}")
            for number, user in enumerate(users)
        ),
        return_exceptions=True,
    )
    assert sum(outcome is None for outcome in outcomes) == 1
    assert all(o is None or isinstance(o, FirstAdminRefusedError) for o in outcomes)
    async with committed_sessionmaker() as session, session.begin():
        admins = await session.scalar(
            select(func.count()).select_from(Staff).where(Staff.role == StaffRole.ADMIN)
        )
    assert admins == 1


async def test_a_run_waits_for_the_lock(
    committed_sessionmaker: async_sessionmaker[AsyncSession], database: DatabaseUrls
) -> None:
    """The check and the write happen under one lock: a second run cannot slip between."""
    holder = await asyncpg.connect(database.app, timeout=3)
    try:
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute("SELECT pg_advisory_xact_lock($1)", FIRST_ADMIN_LOCK_ID)
        run = asyncio.create_task(
            create_first_admin(committed_sessionmaker, FakeAuth(USER), USER.id, "Abebe")
        )
        await asyncio.sleep(0.3)
        assert not run.done()  # blocked on the lock
        await transaction.rollback()
        await asyncio.wait_for(run, timeout=5)
    finally:
        await holder.close()
    assert len(await _staff(committed_sessionmaker)) == 1


# --- the command line ---------------------------------------------------------------------


@pytest.fixture
async def command_env(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> list[httpx.Request]:
    """`main` with settings for the test database and Supabase on a mock transport."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(f"/admin/users/{USER.id}"):
            return httpx.Response(200, json=user_json(id=str(USER.id)))
        return httpx.Response(404, json={"error_code": "user_not_found"})

    settings = make_settings(database_url=database.app, supabase_secret_key=SECRET_KEY)
    monkeypatch.setattr(first_admin, "load_settings", lambda: settings)
    monkeypatch.setattr(
        first_admin,
        "OutboundClient",
        lambda **options: OutboundClient(
            **options,
            resolver=public_resolver,
            transport_factory=lambda: httpx.MockTransport(handler),
        ),
    )
    return requests  # committed_sessionmaker empties the tables afterwards


async def _main(*argv: str) -> int:
    # main() runs its own event loop, as from a shell.
    return await asyncio.to_thread(first_admin.main, list(argv))


async def test_the_command_makes_the_first_admin(
    command_env: list[httpx.Request],
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await _main("--user-id", str(USER.id), "--name", "  Abebe Kebede ")
    assert code == 0
    (staff,) = await _staff(committed_sessionmaker)
    assert (staff.full_name, staff.email, staff.role) == (
        "Abebe Kebede",
        "abebe@example.com",
        StaffRole.ADMIN,
    )
    output = capsys.readouterr()
    assert str(USER.id) in output.err
    assert SECRET_KEY not in output.out + output.err
    assert "abebe" not in (output.out + output.err).lower()  # no personal data in the output


async def test_the_command_reports_a_refusal(
    command_env: list[httpx.Request], capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main("--user-id", str(uuid.uuid4()), "--name", "Nobody") == 1
    assert "no Supabase user" in capsys.readouterr().err


async def test_the_command_reports_supabase_errors(
    command_env: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def down(self: object, user_id: uuid.UUID) -> AuthUser | None:
        raise AuthAdminError("could not reach Supabase")

    monkeypatch.setattr(SupabaseAuthAdmin, "get_user", down)
    assert await _main("--user-id", str(USER.id), "--name", "Abebe") == 1
    err = capsys.readouterr().err
    assert "could not reach Supabase" in err
    assert "nothing was changed" in err


async def test_the_command_needs_the_secret_key(
    command_env: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(first_admin, "load_settings", lambda: make_settings())
    assert await _main("--user-id", str(USER.id), "--name", "Abebe") == 2
    assert "SUPABASE_SECRET_KEY" in capsys.readouterr().err
    assert command_env == []  # Supabase was never called


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--user-id", "not-a-uuid", "--name", "Abebe"],
        ["--user-id", str(USER.id)],
        ["--user-id", str(USER.id), "--name", "   "],
        ["--user-id", str(USER.id), "--name", "x" * 201],
        ["--user-id", str(USER.id), "--name", "Abebe\x1b[31m"],
        ["--user-id", str(USER.id), "--name", "Abebe", "--extra"],
    ],
)
async def test_bad_arguments_are_usage_errors(
    command_env: list[httpx.Request], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        await _main(*argv)
    assert exc_info.value.code == 2
    assert command_env == []


async def test_bad_settings_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise ConfigError("DATABASE_URL: field required")

    monkeypatch.setattr(first_admin, "load_settings", broken)
    assert await _main("--user-id", str(USER.id), "--name", "Abebe") == 2
    assert "DATABASE_URL" in capsys.readouterr().err


async def test_the_command_reports_the_database_down(
    command_env: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(database_url=UNREACHABLE_DATABASE_URL, supabase_secret_key=SECRET_KEY)
    monkeypatch.setattr(first_admin, "load_settings", lambda: settings)
    assert await _main("--user-id", str(USER.id), "--name", "Abebe") == 1
    assert "database is unavailable; nothing was changed" in capsys.readouterr().err


async def test_other_errors_are_not_hidden(
    command_env: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(first_admin, "create_first_admin", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        await _main("--user-id", str(USER.id), "--name", "Abebe")
