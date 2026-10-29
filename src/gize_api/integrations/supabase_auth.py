"""Supabase Auth's admin API, called with the project's secret key (GZ-014).

Only what Gize needs: read a user (the first-admin command; staff invites in GZ-013 add
more). The key travels in the `apikey` header only: the new secret keys are not JWTs, and
as a Bearer token Supabase would reject them. Errors never contain the key or the user's
data, and every call goes through the guarded outbound client to the project's host only.
"""

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import SecretStr

from gize_api.core.config import Settings
from gize_api.core.http import OutboundClient, OutboundError
from gize_api.core.telemetry import Telemetry


class AuthAdminError(Exception):
    """Supabase could not answer: unreachable, the key refused, or an unexpected response."""


@dataclass(frozen=True)
class AuthUser:
    """The parts of a Supabase user that decide whether they may be staff."""

    id: uuid.UUID
    email: str | None  # trimmed and lower-case, as gize.staff stores it
    email_confirmed: bool
    banned: bool
    deleted: bool
    anonymous: bool


class SupabaseAuthAdmin:
    def __init__(self, *, admin_url: str, secret_key: SecretStr, client: OutboundClient) -> None:
        self._admin_url = admin_url
        self._secret_key = secret_key
        self._client = client

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        telemetry: Telemetry | None,
        outbound: OutboundClient | None = None,
    ) -> Self:
        if settings.supabase_secret_key is None:
            raise ValueError("set SUPABASE_SECRET_KEY to call Supabase's admin API")
        client = outbound or OutboundClient(
            allowed_hosts={settings.supabase_host}, telemetry=telemetry
        )
        return cls(
            admin_url=f"{settings.supabase_auth_issuer}/admin",
            secret_key=settings.supabase_secret_key,
            client=client,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_user(self, user_id: uuid.UUID) -> AuthUser | None:
        """The user with this ID, or None if Supabase has none."""
        status, body = await self._get(f"/users/{user_id}")
        if status == 404:
            return None
        if status != 200:
            raise AuthAdminError(f"Supabase answered with status {status}")
        return _user(body, user_id)

    async def _get(self, path: str) -> tuple[int, Any]:
        try:
            response = await self._client.request(
                "GET",
                f"{self._admin_url}{path}",
                headers={"apikey": self._secret_key.get_secret_value()},
            )
        except OutboundError:
            raise AuthAdminError("could not reach Supabase") from None
        if response.status in {401, 403}:
            raise AuthAdminError("Supabase refused the secret key (check SUPABASE_SECRET_KEY)")
        if response.status != 200:
            return response.status, None
        try:
            return 200, response.json()
        except ValueError:
            raise AuthAdminError("unexpected response from Supabase") from None


def _user(body: Any, user_id: uuid.UUID) -> AuthUser:  # noqa: ANN401 - JSON
    unexpected = AuthAdminError("unexpected response from Supabase")
    if not isinstance(body, dict):
        raise unexpected
    try:
        if uuid.UUID(str(body.get("id"))) != user_id:
            raise unexpected
    except ValueError:
        raise unexpected from None
    email = body.get("email")
    anonymous = body.get("is_anonymous", False)  # older Auth versions leave it out
    if (email is not None and not isinstance(email, str)) or not isinstance(anonymous, bool):
        raise unexpected
    return AuthUser(
        id=user_id,
        email=(email.strip().lower() or None) if email else None,
        email_confirmed=_timestamp(body.get("email_confirmed_at")) is not None,
        banned=_banned(body.get("banned_until")),
        deleted=_timestamp(body.get("deleted_at")) is not None,
        anonymous=anonymous,
    )


def _timestamp(value: object) -> datetime | None:
    """A timestamp field: absent (None) or a readable time; anything else is unexpected."""
    if value is None:
        return None
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value)
    raise AuthAdminError("unexpected response from Supabase")


def _banned(until: object, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> bool:
    """Banned until a time still to come; an unreadable value counts as banned."""
    if until is None:
        return False
    if not isinstance(until, str):
        return True
    try:
        moment = datetime.fromisoformat(until)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment > now()


__all__ = ["AuthAdminError", "AuthUser", "SupabaseAuthAdmin"]
