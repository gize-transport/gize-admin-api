"""Staff-management test helpers (GZ-013): a fake Supabase invite endpoint and shortcuts."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.audit import AuditEntry
from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import SupabaseAuthAdmin
from gize_api.staff.models import Staff, StaffRole
from tests.factories import StaffFactory
from tests.supabase import SECRET_KEY, user_json
from tests.support import SettingsFactory, public_resolver
from tests.tokens import bearer, make_token

STAFF = "/v1/staff"


class FakeSupabase:
    """Supabase's invite endpoint on a mock transport.

    Each email keeps one user ID, as in Supabase: inviting a user who has not accepted yet
    sends the invite again and returns the same user. `answer` replaces the reply;
    `before_answer` runs first (to change the database while the call is in flight).
    """

    def __init__(self) -> None:
        self.invites: list[dict[str, Any]] = []
        self.ids: dict[str, uuid.UUID] = {}
        self.answer: Callable[[httpx.Request], httpx.Response] | None = None
        self.before_answer: Callable[[], Awaitable[None]] | None = None

    def user_id(self, email: str) -> uuid.UUID:
        return self.ids.setdefault(email.lower(), uuid.uuid4())

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.invites.append(body)
        if self.before_answer is not None:
            await self.before_answer()
        if self.answer is not None:
            return self.answer(request)
        user = user_json(
            id=str(self.user_id(body["email"])), email=body["email"], email_confirmed_at=None
        )
        return httpx.Response(200, json=user)

    def client(self, make_settings: SettingsFactory) -> SupabaseAuthAdmin:
        outbound = OutboundClient(
            allowed_hosts={make_settings().supabase_host},
            resolver=public_resolver,
            transport_factory=lambda: httpx.MockTransport(self.handle),
        )
        return SupabaseAuthAdmin.for_settings(
            make_settings(supabase_secret_key=SECRET_KEY), telemetry=None, outbound=outbound
        )


async def add_staff(session: AsyncSession, **values: Any) -> Staff:  # noqa: ANN401
    """A committed (or, in `db_session`, rolled-back-later) staff row."""
    async with session.begin():
        return await StaffFactory().create(session, **values)


async def add_admin(session: AsyncSession, **values: Any) -> Staff:  # noqa: ANN401
    return await add_staff(session, role=StaffRole.ADMIN, **values)


def signed_in(staff_id: uuid.UUID) -> dict[str, str]:
    return bearer(make_token(sub=str(staff_id)))


def with_key(staff_id: uuid.UUID, key: uuid.UUID | None = None) -> dict[str, str]:
    """Signed in, with an Idempotency-Key (a new one unless given)."""
    return {**signed_in(staff_id), "Idempotency-Key": str(key or uuid.uuid4())}


async def audit_entries(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditEntry]:
    async with session.begin():
        statement = (
            select(AuditEntry)
            .where(AuditEntry.entity_id == entity_id)
            .order_by(AuditEntry.created_at, AuditEntry.action)
        )
        return list((await session.scalars(statement)).all())


async def active_admins(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session, session.begin():
        statement = select(func.count()).where(Staff.role == StaffRole.ADMIN, Staff.is_active)
        return int(await session.scalar(statement.select_from(Staff)) or 0)
