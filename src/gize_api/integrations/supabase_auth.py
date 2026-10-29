"""Supabase Auth's admin API, called with the project's secret key.

Only what Gize needs: read a user (the first-admin command, GZ-014) and invite one (staff
management, GZ-013). The key travels in the `apikey` header only: the new secret keys are
not JWTs, and as a Bearer token Supabase would reject them. Errors never contain the key
or the user's data, and every call goes through the guarded outbound client to the
project's host only. Invites are POSTs, which the client never retries: each sends an email.
"""

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import SecretStr

from gize_api.core.config import Settings
from gize_api.core.http import OutboundClient, OutboundError, OutboundResponse
from gize_api.core.telemetry import Telemetry


class AuthAdminError(Exception):
    """Supabase could not answer: unreachable, the key refused, or an unexpected response."""


class AuthAccountExistsError(Exception):
    """The email belongs to a Supabase user who has already confirmed it: nothing was sent."""


class AuthEmailRejectedError(Exception):
    """Supabase does not accept the email address: nothing was sent."""


# Supabase's error codes (the `error_code` field of its error responses).
_ACCOUNT_EXISTS = frozenset({"email_exists", "user_already_exists"})
_EMAIL_REJECTED = frozenset({"email_address_invalid", "validation_failed"})
_EMAIL_NOT_AUTHORIZED = "email_address_not_authorized"


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
    def __init__(self, *, auth_url: str, secret_key: SecretStr, client: OutboundClient) -> None:
        self._auth_url = auth_url
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
            auth_url=settings.supabase_auth_issuer,
            secret_key=settings.supabase_secret_key,
            client=client,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_user(self, user_id: uuid.UUID) -> AuthUser | None:
        """The user with this ID, or None if Supabase has none."""
        response = await self._call("GET", f"/admin/users/{user_id}")
        if response.status == 404:
            return None
        if response.status != 200:
            raise AuthAdminError(f"Supabase answered with status {response.status}")
        return _user(_json(response), user_id)

    async def invite_user(self, email: str, *, full_name: str) -> uuid.UUID:
        """Send an invitation email; the ID of the user it creates.

        An email whose user has not accepted an earlier invite is invited again, and the
        same user ID comes back: a retry after a failure is safe. `email` is trimmed and
        lower-case; the name becomes the user's display name in the Supabase dashboard
        (gize.staff holds the name the API uses).
        """
        response = await self._call(
            "POST", "/invite", json={"email": email, "data": {"full_name": full_name}}
        )
        if response.status != 200:
            error_code = _error_code(response)
            if response.status == 422 and error_code in _ACCOUNT_EXISTS:
                raise AuthAccountExistsError("the email already has a confirmed account")
            if response.status in {400, 422} and error_code in _EMAIL_REJECTED:
                raise AuthEmailRejectedError("Supabase does not accept the email address")
            if error_code == _EMAIL_NOT_AUTHORIZED:
                raise AuthAdminError(
                    "Supabase may not email this address: set up custom SMTP (README)"
                )
            raise AuthAdminError(f"Supabase answered with status {response.status}")
        body = _json(response)
        user_id = _uuid(body.get("id") if isinstance(body, dict) else None)
        invited = _user(body, user_id)
        if invited.email != email:
            raise AuthAdminError("unexpected response from Supabase")
        return user_id

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,  # noqa: ANN401 - JSON
    ) -> OutboundResponse:
        try:
            response = await self._client.request(
                method,
                f"{self._auth_url}{path}",
                json=json,
                headers={"apikey": self._secret_key.get_secret_value()},
            )
        except OutboundError:
            raise AuthAdminError("could not reach Supabase") from None
        if response.status in {401, 403}:
            raise AuthAdminError("Supabase refused the secret key (check SUPABASE_SECRET_KEY)")
        return response


def _json(response: OutboundResponse) -> Any:  # noqa: ANN401 - JSON
    try:
        return response.json()
    except ValueError:
        raise AuthAdminError("unexpected response from Supabase") from None


def _error_code(response: OutboundResponse) -> str | None:
    """Supabase's `error_code` from an error response, if it has a readable one."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error_code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _uuid(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise AuthAdminError("unexpected response from Supabase") from None


def _user(body: Any, user_id: uuid.UUID) -> AuthUser:  # noqa: ANN401 - JSON
    unexpected = AuthAdminError("unexpected response from Supabase")
    if not isinstance(body, dict) or _uuid(body.get("id")) != user_id:
        raise unexpected
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


__all__ = [
    "AuthAccountExistsError",
    "AuthAdminError",
    "AuthEmailRejectedError",
    "AuthUser",
    "SupabaseAuthAdmin",
]
