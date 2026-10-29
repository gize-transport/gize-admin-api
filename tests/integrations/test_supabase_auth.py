"""GZ-014: reading users from Supabase Auth's admin API with the project's secret key."""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import AuthAdminError, AuthUser, SupabaseAuthAdmin
from tests.supabase import SECRET_KEY, USER_ID, user_json
from tests.support import TEST_SUPABASE_URL, SettingsFactory, public_resolver

Handler = Callable[[httpx.Request], httpx.Response]


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
        ({"is_anonymous": False}, "anonymous", False),
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
        (httpx.Response(200, json=user_json(email_confirmed_at="")), "unexpected response"),
        (httpx.Response(200, json=user_json(email_confirmed_at=False)), "unexpected response"),
        (httpx.Response(200, json=user_json(email_confirmed_at="soon")), "unexpected response"),
        (httpx.Response(200, json=user_json(deleted_at=1)), "unexpected response"),
        (httpx.Response(200, json=user_json(deleted_at="")), "unexpected response"),
        (httpx.Response(200, json=user_json(is_anonymous="true")), "unexpected response"),
        (httpx.Response(200, json=user_json(is_anonymous=None)), "unexpected response"),
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


async def test_the_real_client_talks_only_to_the_project(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []

    def outbound(**options: Any) -> OutboundClient:  # noqa: ANN401
        created.append(options)
        return OutboundClient(**options)

    monkeypatch.setattr("gize_api.integrations.supabase_auth.OutboundClient", outbound)
    admin = SupabaseAuthAdmin.for_settings(
        make_settings(supabase_secret_key=SECRET_KEY), telemetry=None
    )
    await admin.aclose()
    assert [options["allowed_hosts"] for options in created] == [{make_settings().supabase_host}]


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


async def test_a_record_without_is_anonymous_is_not_anonymous(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
) -> None:
    body = user_json()
    del body["is_anonymous"]  # older Auth versions leave it out
    user = await respond(httpx.Response(200, json=body)).get_user(USER_ID)
    assert user is not None
    assert not user.anonymous


@pytest.mark.parametrize(("offset_minutes", "banned"), [(5, True), (-60, False)])
async def test_a_ban_with_a_time_zone_is_compared_correctly(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin], offset_minutes: int, banned: bool
) -> None:
    """Addis Ababa time, +03:00. A ban that ended an hour ago would still look active for
    two more hours if the offset were ignored and the time read as UTC."""
    addis = timezone(timedelta(hours=3))
    until = (datetime.now(addis) + timedelta(minutes=offset_minutes)).isoformat()
    user = await respond(httpx.Response(200, json=user_json(banned_until=until))).get_user(USER_ID)
    assert user is not None
    assert user.banned is banned
