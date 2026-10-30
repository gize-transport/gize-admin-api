"""GZ-013: staff changes running at the same time, on committed rows.

Every staff change takes one lock (the first-admin command takes it too), so the last-admin
rule and the caller's own status are checked with nothing changing underneath. The tests
hold that lock themselves until the requests are queued behind it, so they really overlap.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import asyncpg
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.audit import AuditEntry
from gize_api.staff.models import Staff, StaffRole
from gize_api.staff.repository import STAFF_CHANGES_LOCK_ID
from tests.staff.support import (
    STAFF,
    active_admins,
    add_admin,
    add_staff,
    signed_in,
    with_key,
)
from tests.supabase import FakeSupabase
from tests.support import DatabaseUrls, wait_for_lock_waiters

ALMAZ = {"email": "almaz@example.com", "full_name": "Almaz Tesfaye", "role": "staff"}


@contextlib.asynccontextmanager
async def _holding_the_staff_lock(database: DatabaseUrls) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", STAFF_CHANGES_LOCK_ID)
        try:
            yield conn
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", STAFF_CHANGES_LOCK_ID)
    finally:
        await conn.close()


async def _count(sessions: async_sessionmaker[AsyncSession], model: type[object]) -> int:
    async with sessions() as session, session.begin():
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_two_admins_stepping_down_at_once_leave_one(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """Each alone may step down; both at once would leave no admin."""
    async with committed_sessionmaker() as session:
        first, second = await add_admin(session), await add_admin(session)
    async with _holding_the_staff_lock(database):
        requests = [
            asyncio.create_task(
                committed_api.patch(
                    f"{STAFF}/{admin.id}", json={"role": "staff"}, headers=signed_in(admin.id)
                )
            )
            for admin in (first, second)
        ]
        await wait_for_lock_waiters(database, count=2)
    statuses = sorted(response.status_code for response in await asyncio.gather(*requests))
    assert statuses == [200, 409]
    assert await active_admins(committed_sessionmaker) == 1


async def test_two_admins_demoting_each_other_at_once_leave_one(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        first, second = await add_admin(session), await add_admin(session)
    async with _holding_the_staff_lock(database):
        requests = [
            asyncio.create_task(
                committed_api.patch(
                    f"{STAFF}/{target.id}", json={"role": "staff"}, headers=signed_in(caller.id)
                )
            )
            for caller, target in ((first, second), (second, first))
        ]
        await wait_for_lock_waiters(database, count=2)
    responses = await asyncio.gather(*requests)
    # The second is no longer an admin by the time its turn comes.
    assert sorted(response.status_code for response in responses) == [200, 403]
    assert await active_admins(committed_sessionmaker) == 1


async def test_a_caller_deactivated_while_waiting_is_refused(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    async with committed_sessionmaker() as session:
        await add_admin(session)
        caller, member = await add_admin(session), await add_staff(session)
    async with _holding_the_staff_lock(database) as conn:
        request = asyncio.create_task(
            committed_api.patch(
                f"{STAFF}/{member.id}", json={"role": "admin"}, headers=signed_in(caller.id)
            )
        )
        await wait_for_lock_waiters(database)
        await conn.execute("UPDATE gize.staff SET is_active = false WHERE id = $1", caller.id)
    assert (await request).status_code == 403
    async with committed_sessionmaker() as session, session.begin():
        assert (await session.get_one(Staff, member.id)).role is StaffRole.STAFF
    assert await _count(committed_sessionmaker, AuditEntry) == 0


async def test_an_invite_by_a_caller_deactivated_meanwhile_stores_nothing(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    fake_supabase: FakeSupabase,
) -> None:
    """Deactivated while Supabase sent the invite: no staff row, and the key is free.

    Supabase keeps an unconfirmed user nobody can use: without a staff row, signing in
    gets them nothing.
    """
    async with committed_sessionmaker() as session:
        await add_admin(session)
        caller = await add_admin(session)

    async def deactivate_the_caller() -> None:
        conn = await asyncpg.connect(database.migrator, timeout=3)
        try:
            await conn.execute(
                "UPDATE gize.staff SET is_active = $2 WHERE id = $1", caller.id, False
            )
        finally:
            await conn.close()

    fake_supabase.before_answer = deactivate_the_caller
    headers = with_key(caller.id)
    assert (await committed_api.post(STAFF, json=ALMAZ, headers=headers)).status_code == 403
    assert await _count(committed_sessionmaker, Staff) == 2
    assert await _count(committed_sessionmaker, AuditEntry) == 0

    fake_supabase.before_answer = None
    async with committed_sessionmaker() as session, session.begin():
        (await session.get_one(Staff, caller.id)).is_active = True
    assert (await committed_api.post(STAFF, json=ALMAZ, headers=headers)).status_code == 201


async def test_an_email_that_joins_staff_during_the_invite_is_409(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    fake_supabase: FakeSupabase,
) -> None:
    """Checked again under the lock: another admin added this email meanwhile."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)

    async def another_admin_adds_them() -> None:
        conn = await asyncpg.connect(database.migrator, timeout=3)
        try:
            await conn.execute(
                "INSERT INTO gize.staff (id, full_name, email, role)"
                " VALUES (gen_random_uuid(), 'Almaz', 'almaz@example.com', 'staff')"
            )
        finally:
            await conn.close()

    fake_supabase.before_answer = another_admin_adds_them
    response = await committed_api.post(STAFF, json=ALMAZ, headers=with_key(admin.id))
    assert response.status_code == 409
    assert response.json()["code"] == "STAFF_EMAIL_TAKEN"
    assert await _count(committed_sessionmaker, Staff) == 2
    assert await _count(committed_sessionmaker, AuditEntry) == 0


async def test_parallel_invites_with_one_key_create_one_member(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
) -> None:
    """A double-click: both requests reach Supabase (one email is sent twice, harmlessly),
    one creates the member and the other replays its response."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
    headers = with_key(admin.id)
    async with _holding_the_staff_lock(database):
        requests = [
            asyncio.create_task(committed_api.post(STAFF, json=ALMAZ, headers=headers))
            for _ in range(2)
        ]
        # One holds the key and waits for the lock; the other waits for the key.
        await wait_for_lock_waiters(database, count=2)
    first, second = await asyncio.gather(*requests)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    replayed = sorted(
        str(response.headers.get("Idempotent-Replayed")) for response in (first, second)
    )
    assert replayed == ["None", "true"]
    assert await _count(committed_sessionmaker, Staff) == 2
    assert await _count(committed_sessionmaker, AuditEntry) == 1


async def test_a_retry_after_the_database_failed_completes_the_invite(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    fake_supabase: FakeSupabase,
) -> None:
    """Supabase sent the invite, then the write failed (here: the lock wait timed out).
    The retry asks Supabase again, gets the same user, and finishes the job."""
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
    headers = with_key(admin.id)
    async with contextlib.AsyncExitStack() as stack:

        async def hold_the_lock() -> None:
            fake_supabase.before_answer = None  # only the first time
            await stack.enter_async_context(_holding_the_staff_lock(database))

        fake_supabase.before_answer = hold_the_lock
        failed = await committed_api.post(STAFF, json=ALMAZ, headers=headers)
    assert failed.status_code == 503
    assert failed.headers["Retry-After"]
    assert await _count(committed_sessionmaker, Staff) == 1

    retried = await committed_api.post(STAFF, json=ALMAZ, headers=headers)
    assert retried.status_code == 201
    assert "Idempotent-Replayed" not in retried.headers
    assert retried.json()["id"] == str(fake_supabase.user_id("almaz@example.com"))
    assert len(fake_supabase.invites) == 2
    assert await _count(committed_sessionmaker, Staff) == 2
    assert await _count(committed_sessionmaker, AuditEntry) == 1
