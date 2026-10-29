"""Supabase Auth's admin API with the project's secret key: reading users (GZ-014) and
inviting staff (GZ-013)."""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from gize_api.core.http import OutboundClient
from gize_api.integrations.supabase_auth import (
    AuthAccountExistsError,
    AuthAdminError,
    AuthEmailRejectedError,
    AuthUser,
    SupabaseAuthAdmin,
)
from gize_api.main import create_app
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


def _invited(**overrides: Any) -> httpx.Response:  # noqa: ANN401 - JSON
    """Supabase's answer to an invite: the new (or still unconfirmed) user."""
    fields = {"email": "abebe@example.com", "email_confirmed_at": None, **overrides}
    return httpx.Response(200, json=user_json(**fields))


async def test_an_invite_sends_the_email_and_name_with_the_secret_key(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin], requests: list[httpx.Request]
) -> None:
    admin = respond(_invited())
    user_id = await admin.invite_user("abebe@example.com", full_name="Abebe Kebede")
    assert user_id == USER_ID
    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == "/auth/v1/invite"  # not under /admin
    assert request.headers["apikey"] == SECRET_KEY
    assert "authorization" not in request.headers
    # The name becomes the user's display name in the Supabase dashboard; the API itself
    # never reads it back (gize.staff holds the name).
    assert json.loads(request.content) == {
        "email": "abebe@example.com",
        "data": {"full_name": "Abebe Kebede"},
    }


async def test_an_invite_accepts_the_email_in_another_case(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
) -> None:
    admin = respond(_invited(email="Abebe@Example.COM"))
    assert await admin.invite_user("abebe@example.com", full_name="Abebe") == USER_ID


@pytest.mark.parametrize("error_code", ["email_exists", "user_already_exists"])
async def test_an_email_with_a_confirmed_account_is_refused(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin], error_code: str
) -> None:
    body = {"code": 422, "error_code": error_code, "msg": "already been registered"}
    admin = respond(httpx.Response(422, json=body))
    with pytest.raises(AuthAccountExistsError):
        await admin.invite_user("abebe@example.com", full_name="Abebe")


@pytest.mark.parametrize(
    ("status", "error_code"),
    [(400, "email_address_invalid"), (400, "validation_failed"), (422, "validation_failed")],
)
async def test_an_email_supabase_will_not_take_is_rejected(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin], status: int, error_code: str
) -> None:
    admin = respond(httpx.Response(status, json={"code": status, "error_code": error_code}))
    with pytest.raises(AuthEmailRejectedError):
        await admin.invite_user("abebe@example.com", full_name="Abebe")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        # Supabase's built-in email only sends to the project team: custom SMTP is needed.
        (
            httpx.Response(400, json={"code": 400, "error_code": "email_address_not_authorized"}),
            "custom SMTP",
        ),
        (
            httpx.Response(429, json={"code": 429, "error_code": "over_email_send_rate_limit"}),
            "status 429",
        ),
        (httpx.Response(401, json={"msg": "Invalid API key"}), "refused the secret key"),
        (httpx.Response(403, json={"msg": "forbidden"}), "refused the secret key"),
        (httpx.Response(500), "status 500"),
        (httpx.Response(400, json={"code": 400, "error_code": "something_new"}), "status 400"),
        (httpx.Response(422, content=b"<html>"), "status 422"),
        (httpx.Response(422, json=["email_exists"]), "status 422"),
        (httpx.Response(200, content=b"<html>"), "unexpected response"),
        (httpx.Response(200, json={"email": "abebe@example.com"}), "unexpected response"),
        (_invited(id="nope"), "unexpected response"),
        (_invited(email="someone.else@example.com"), "unexpected response"),
        (_invited(email=None), "unexpected response"),
    ],
)
async def test_invite_failures_are_errors_without_the_key(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
    requests: list[httpx.Request],
    response: httpx.Response,
    message: str,
) -> None:
    admin = respond(response)
    with pytest.raises(AuthAdminError, match=message) as exc_info:
        await admin.invite_user("abebe@example.com", full_name="Abebe Kebede")
    assert SECRET_KEY not in str(exc_info.value)
    assert "abebe" not in str(exc_info.value).lower()  # no personal data in errors
    assert len(requests) == 1  # an invite sends an email: never retried blindly


async def test_supabase_unreachable_during_an_invite_is_an_error(
    make_settings: SettingsFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    admin = admin_for(make_settings, handler)
    try:
        with pytest.raises(AuthAdminError, match="could not reach"):
            await admin.invite_user("abebe@example.com", full_name="Abebe")
    finally:
        await admin.aclose()


async def test_the_app_has_an_admin_client_only_with_the_secret_key(
    make_settings: SettingsFactory,
) -> None:
    """Staff invites use it; without the key they answer 503 and the rest works."""
    with_key = create_app(make_settings(supabase_secret_key=SECRET_KEY))
    async with with_key.router.lifespan_context(with_key):
        assert isinstance(with_key.state.auth_admin, SupabaseAuthAdmin)
    assert not hasattr(with_key.state, "auth_admin")  # closed and dropped at shutdown
    without = create_app(make_settings())
    async with without.router.lifespan_context(without):
        assert without.state.auth_admin is None


async def test_a_failing_admin_client_close_does_not_stop_shutdown(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failing(self: SupabaseAuthAdmin) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(SupabaseAuthAdmin, "aclose", failing)
    app = create_app(make_settings(supabase_secret_key=SECRET_KEY))
    async with app.router.lifespan_context(app):
        pass
    assert '"auth_admin_close_failed"' in capsys.readouterr().err


@pytest.mark.parametrize(
    ("response", "retry_after"),
    [
        (httpx.Response(500), 30),
        (httpx.Response(429), 60),
        (httpx.Response(429, headers={"Retry-After": "120"}), 120),
        (httpx.Response(429, headers={"Retry-After": "0"}), 1),
        (httpx.Response(429, headers={"Retry-After": "86400"}), 3600),
        (httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 60),
        (httpx.Response(401), None),  # a configuration fix, not a wait
        (httpx.Response(400, json={"error_code": "email_address_not_authorized"}), None),
    ],
)
async def test_invite_errors_say_how_long_to_wait(
    respond: Callable[[httpx.Response], SupabaseAuthAdmin],
    response: httpx.Response,
    retry_after: int | None,
) -> None:
    admin = respond(response)
    with pytest.raises(AuthAdminError) as exc_info:
        await admin.invite_user("abebe@example.com", full_name="Abebe")
    assert exc_info.value.retry_after == retry_after
