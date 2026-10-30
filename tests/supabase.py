"""Supabase Auth admin API test data: a made-up secret key, user records, and a fake invite
endpoint on a mock transport (`FakeSupabase`)."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import SupabaseAuthAdmin
from tests.support import SettingsFactory, public_resolver

SECRET_KEY = "sb_secret_" + "x" * 32  # a made-up key: the shape only
USER_ID = uuid.UUID(int=42)


def user_json(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - JSON
    """A user as Supabase's admin API returns it (the fields we read, plus some we don't)."""
    user: dict[str, Any] = {
        "id": str(USER_ID),
        "aud": "authenticated",
        "role": "authenticated",
        "email": "Abebe@Example.com",
        "email_confirmed_at": "2026-10-01T08:00:00.123456Z",
        "phone": "",
        "banned_until": None,
        "deleted_at": None,
        "is_anonymous": False,
        "app_metadata": {"provider": "email"},
        "user_metadata": {},
    }
    user.update(overrides)
    return user


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
