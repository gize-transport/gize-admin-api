"""GZ-014: reading users from Supabase Auth's admin API with the project's secret key."""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import AuthAdminError, AuthUser, SupabaseAuthAdmin
from tests.support import TEST_SUPABASE_URL, SettingsFactory, public_resolver

SECRET_KEY = "sb_secret_" + "x" * 32  # a made-up key: the shape only
USER_ID = uuid.UUID(int=42)
Handler = Callable[[httpx.Request], httpx.Response]


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


def admin_for(make_settings: SettingsFactory, handler: Handler) -> SupabaseAuthAdmin:
    client = OutboundClient(
        allowed_hosts={make_settings().supabase_host},
        resolver=public_resolver,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    return SupabaseAuthAdmin.for_settings(
        make_settings(supabase_secret_key=SECRET_KEY), telemetry=None, outbound=client
    )


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
async def respond(
    make_settings: SettingsFactory, requests: list[httpx.Request]
) -> AsyncIterator[Callable[[httpx.Response], SupabaseAuthAdmin]]:
    """An admin client whose every request gets the given response."""
    made: list[SupabaseAuthAdmin] = []

    def build(response: httpx.Response) -> SupabaseAuthAdmin:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return response

        made.append(admin_for(make_settings, handler))
        return made[-1]

    yield build
    for admin in made:
        await admin.aclose()


async def test_a_user_is_read_with_the_secret_key(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin], requests: list[httpx.Request]
) -> None:
    admin = respond(httpx.Response(200, json=user_json()))
    user = await admin.get_user(USER_ID)
    assert user == AuthUser(
        id=USER_ID,
        email="abebe@example.com",  # trimmed and lower-cased, as the staff table stores it
        email_confirmed=True,
        banned=False,
        deleted=False,
        anonymous=False,
    )
    (request,) = requests
    assert request.method == "GET"
    # The client connects to the checked address and names the project's host.
    assert request.url.path == f"/auth/v1/admin/users/{USER_ID}"
    assert request.headers["host"] == TEST_SUPABASE_URL.removeprefix("https://")
    assert request.headers["apikey"] == SECRET_KEY
    # The new secret keys are not JWTs: as a Bearer token Supabase would reject them.
    assert "authorization" not in request.headers


async def test_an_unknown_user_is_none(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
) -> None:
    admin = respond(httpx.Response(404, json={"code": 404, "error_code": "user_not_found"}))
    assert await admin.get_user(USER_ID) is None


@pytest.mark.parametrize(
    ("overrides", "field", "value"),
    [
        ({"email": None}, "email", None),
        ({"email": ""}, "email", None),
        ({"email_confirmed_at": None}, "email_confirmed", False),
        ({"deleted_at": "2026-10-02T08:00:00Z"}, "deleted", True),
        ({"is_anonymous": True}, "anonymous", True),
        ({"banned_until": "2999-01-01T00:00:00Z"}, "banned", True),
        ({"banned_until": "2000-01-01T00:00:00Z"}, "banned", False),  # a ban that ended
        ({"banned_until": "2999-01-01T00:00:00"}, "banned", True),  # no zone: read as UTC
        ({"banned_until": "not a date"}, "banned", True),  # unreadable: assume the worst
        ({"banned_until": 12}, "banned", True),
    ],
)
async def test_what_the_user_record_says(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
    overrides: dict[str, Any],
    field: str,
    value: object,
) -> None:
    admin = respond(httpx.Response(200, json=user_json(**overrides)))
    user = await admin.get_user(USER_ID)
    assert user is not None
    assert getattr(user, field) == value


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"msg": "Invalid API key"}), "refused the secret key"),
        (httpx.Response(403, json={"msg": "forbidden"}), "refused the secret key"),
        (httpx.Response(500), "status 500"),
        (httpx.Response(200, content=b"<html>"), "unexpected response"),
        (httpx.Response(200, json=[1, 2]), "unexpected response"),
        (httpx.Response(200, json=user_json(id=str(uuid.UUID(int=1)))), "unexpected response"),
        (httpx.Response(200, json=user_json(id="nope")), "unexpected response"),
        (httpx.Response(200, json=user_json(email=7)), "unexpected response"),
    ],
)
async def test_unexpected_answers_are_errors_without_the_key(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
    response: httpx.Response,
    message: str,
) -> None:
    admin = respond(response)
    with pytest.raises(AuthAdminError, match=message) as exc_info:
        await admin.get_user(USER_ID)
    assert SECRET_KEY not in str(exc_info.value)


async def test_supabase_unreachable_is_an_error(make_settings: SettingsFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    admin = admin_for(make_settings, handler)
    try:
        with pytest.raises(AuthAdminError, match="could not reach"):
            await admin.get_user(USER_ID)
    finally:
        await admin.aclose()


def test_the_secret_key_is_required(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValueError, match="SUPABASE_SECRET_KEY"):
        SupabaseAuthAdmin.for_settings(make_settings(), telemetry=None)


async def test_the_real_client_talks_only_to_the_project(make_settings: SettingsFactory) -> None:
    admin = SupabaseAuthAdmin.for_settings(
        make_settings(supabase_secret_key=SECRET_KEY), telemetry=None
    )
    await admin.aclose()


def test_the_secret_key_never_shows_in_a_repr(make_settings: SettingsFactory) -> None:
    settings = make_settings(supabase_secret_key=SECRET_KEY)
    assert SECRET_KEY not in repr(settings)
    assert SECRET_KEY not in json.dumps(settings.model_dump(mode="json"))


async def test_the_banned_until_time_is_compared_in_utc(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
) -> None:
    soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    admin = respond(httpx.Response(200, json=user_json(banned_until=soon)))
    user = await admin.get_user(USER_ID)
    assert user is not None
    assert user.banned
