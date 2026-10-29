"""Supabase Auth admin API test data: a made-up secret key and user records."""

import uuid
from typing import Any

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
