"""GZ-010: Supabase access tokens are verified against the project's public keys.

Keys and tokens are generated here at runtime (never real tokens, never committed); the
key set is served by a fake fetcher, or through the real outbound client on a mock
transport.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import jwt
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends

from gize_api.core.http import OutboundClient
from gize_api.core.ratelimit import RateLimiter
from gize_api.core.security import (
    InvalidTokenError,
    KeysUnavailableError,
    TokenVerifier,
    VerifiedToken,
    verified_token,
)
from gize_api.main import GizeAPI, create_app
from tests.support import SettingsFactory
from tests.tokens import (
    EC_KEY,
    KEY_SET,
    OTHER_EC_KEY,
    RSA_KEY,
    FakeKeys,
    bearer,
    fake_verifier,
    make_claims,
    make_jwk,
    make_token,
)


@pytest.fixture(autouse=True)
def _fresh_sentry_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security._reported_at", {})


async def test_a_valid_es256_token() -> None:
    result = await fake_verifier().verify(make_token())
    assert result == VerifiedToken(
        subject=uuid.UUID(int=7),
        session_id=str(uuid.UUID(int=9)),
        expires_at=result.expires_at,
    )


async def test_a_valid_rs256_token() -> None:
    assert (
        await fake_verifier().verify(make_token(RSA_KEY, "rsa-1", "RS256"))
    ).subject == uuid.UUID(int=7)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (lambda: make_token(exp=int(time.time()) - 120), "expired"),
        (lambda: make_token(nbf=int(time.time()) + 600), "not_yet_valid"),
        (lambda: make_token(aud="anon"), "audience"),
        (lambda: make_token(iss="https://other.supabase.co/auth/v1"), "issuer"),
        (lambda: make_token(OTHER_EC_KEY), "signature"),  # signed by a key we don't trust
        (lambda: make_token(sub=None), "claims"),
        (lambda: make_token(exp=None), "claims"),
        (lambda: make_token(sub="not-a-uuid"), "claims"),
        (lambda: make_token(role="service_role"), "claims"),
        (lambda: make_token(role="anon"), "claims"),
        (lambda: make_token(is_anonymous=True), "claims"),  # an anonymous sign-in is no one
        (lambda: make_token(exp=10**12), "claims"),  # past what a date can hold
        (lambda: make_token(exp=10**300), "claims"),
        (lambda: make_token(RSA_KEY, "ec-1", "RS256"), "algorithm"),  # alg differs from the key's
        (
            lambda: jwt.encode(
                make_claims(),
                "a-guessed-shared-secret-long-enough-for-hs256",
                algorithm="HS256",
                headers={"kid": "ec-1"},
            ),
            "algorithm",
        ),
        (lambda: jwt.encode(make_claims(), "", algorithm="none"), "algorithm"),
        (lambda: "not.a.token", "malformed"),
        (lambda: make_token(iat="soon"), "malformed"),
        (lambda: jwt.encode(make_claims(), EC_KEY, algorithm="ES256"), "unknown_key"),  # no kid
        (lambda: "x" * 9000, "malformed"),
        (lambda: make_token(kid="nobody"), "unknown_key"),
    ],
)
async def test_bad_tokens_are_rejected(token: Callable[[], str], reason: str) -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        await fake_verifier().verify(token())
    assert exc_info.value.reason == reason


async def test_tampering_with_the_payload_breaks_the_signature() -> None:
    header, _payload, signature = make_token().split(".")
    forged = jwt.utils.base64url_encode(
        json.dumps({**make_claims(), "sub": str(uuid.UUID(int=8))}).encode()
    ).decode()
    with pytest.raises(InvalidTokenError) as exc_info:
        await fake_verifier().verify(f"{header}.{forged}.{signature}")
    assert exc_info.value.reason == "signature"


async def test_a_small_clock_difference_is_tolerated() -> None:
    assert (
        await fake_verifier().verify(make_token(exp=int(time.time()) - 10))
    ).subject == uuid.UUID(int=7)


async def test_keys_are_fetched_once_and_cached() -> None:
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    for _ in range(5):
        await verifier.verify(make_token())
    assert keys.fetches == 1


async def test_unknown_keys_cannot_trigger_more_than_one_fetch_per_interval() -> None:
    """Forged key IDs must not turn into a flood of requests to Supabase."""
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    for kid in ("forged-1", "forged-2", "forged-3"):
        with pytest.raises(InvalidTokenError) as exc_info:
            await verifier.verify(make_token(kid=kid))
        assert exc_info.value.reason == "unknown_key"
    assert keys.fetches == 1  # the last fetch is under REFRESH_MIN_INTERVAL_SECONDS old


async def test_an_unknown_key_refreshes_once_the_interval_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    with pytest.raises(InvalidTokenError):
        await verifier.verify(make_token(kid="forged"))
    assert keys.fetches == 2


async def test_a_rotated_key_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    new_key = ec.generate_private_key(ec.SECP256R1())
    keys.key_set = {"keys": [*KEY_SET["keys"], make_jwk(new_key, "ec-2", "ES256")]}
    assert (await verifier.verify(make_token(new_key, "ec-2"))).subject == uuid.UUID(int=7)


async def test_cached_keys_keep_working_while_supabase_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)  # always due a refresh
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    keys.failing = True
    assert (await verifier.verify(make_token())).subject == uuid.UUID(int=7)


async def test_cached_keys_expire_after_a_long_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    keys.failing = True
    monkeypatch.setattr("gize_api.core.security.KEYS_MAX_STALE_SECONDS", 0.0)
    with pytest.raises(KeysUnavailableError):
        await verifier.verify(make_token())


async def test_no_keys_at_all_is_unavailable_not_a_bad_token() -> None:
    keys = FakeKeys()
    keys.failing = True
    with pytest.raises(KeysUnavailableError):
        await fake_verifier(keys).verify(make_token())


@pytest.mark.parametrize(
    "key_set",
    [
        {"keys": []},  # a project still on the shared secret publishes no public keys
        {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "ec-1", "alg": "HS256"}]},
        {"not": "a key set"},
        ["not", "an", "object"],
        {"keys": [{**KEY_SET["keys"][0], "use": "enc"}]},  # an encryption key
        {"keys": [{**KEY_SET["keys"][0], "key_ops": ["encrypt"]}]},
        {"keys": [{**KEY_SET["keys"][0], "d": "cHJpdmF0ZQ"}]},  # private material: refused
        {"keys": ["not a key", 42]},
        {"keys": [{"kty": "EC", "crv": "P-256", "x": "!!", "y": "!!", "kid": "x", "alg": "ES256"}]},
    ],
)
async def test_unusable_key_sets_are_unavailable(key_set: Any) -> None:  # noqa: ANN401
    with pytest.raises(KeysUnavailableError):
        await fake_verifier(FakeKeys(key_set)).verify(make_token())


async def test_keys_come_from_the_project_through_the_outbound_client(
    make_settings: SettingsFactory,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=KEY_SET)

    settings = make_settings()
    client = OutboundClient(
        allowed_hosts={settings.supabase_host},
        resolver=_public_resolver,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    verifier = TokenVerifier.for_settings(settings, telemetry=None, outbound=client)
    try:
        assert (await verifier.verify(make_token())).subject == uuid.UUID(int=7)
    finally:
        await verifier.aclose()
    assert seen[0].headers["Host"] == "abcdefghijklmnopqrst.supabase.co"
    assert seen[0].url.path == "/auth/v1/.well-known/jwks.json"
    assert "Authorization" not in seen[0].headers  # public keys need no credentials


async def _public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize("status", [404, 500])
async def test_an_error_from_the_key_endpoint_is_unavailable(
    make_settings: SettingsFactory, status: int
) -> None:
    client = OutboundClient(
        allowed_hosts={make_settings().supabase_host},
        resolver=_public_resolver,
        transport_factory=lambda: httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    verifier = TokenVerifier.for_settings(make_settings(), telemetry=None, outbound=client)
    try:
        with pytest.raises(KeysUnavailableError):
            await verifier.verify(make_token())
    finally:
        await verifier.aclose()


# --- the FastAPI dependency ---------------------------------------------------------------


def _app(make_settings: SettingsFactory) -> GizeAPI:
    app = create_app(make_settings())

    @app.get("/_test/me")
    async def me(token: VerifiedToken = Depends(verified_token)) -> dict[str, str]:  # noqa: B008
        return {"sub": str(token.subject)}

    return app


@pytest.fixture
async def client(
    make_settings: SettingsFactory, test_redis_url: str
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeKeys]]:
    app = _app(make_settings)
    keys = FakeKeys()
    prefix = f"gize-test-auth-{uuid.uuid4()}"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        await app.state.token_verifier.aclose()
        app.state.token_verifier = fake_verifier(keys)
        await app.state.rate_limiter.aclose()
        app.state.rate_limiter = RateLimiter.for_settings(
            make_settings(redis_url=test_redis_url), telemetry=None, key_prefix=prefix
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, keys


async def test_a_valid_token_reaches_the_handler(
    client: tuple[httpx.AsyncClient, FakeKeys],
) -> None:
    http, _ = client
    response = await http.get("/_test/me", headers=bearer(make_token()))
    assert response.status_code == 200
    assert response.json() == {"sub": str(uuid.UUID(int=7))}


@pytest.mark.parametrize(
    ("headers", "challenge"),
    [
        ({}, "Bearer"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "Bearer"),
        ({"Authorization": "Bearer"}, "Bearer"),
        (None, 'Bearer error="invalid_token"'),  # an expired token
    ],
)
async def test_missing_or_bad_credentials_are_401(
    client: tuple[httpx.AsyncClient, FakeKeys],
    headers: dict[str, str] | None,
    challenge: str,
) -> None:
    http, _ = client
    if headers is None:
        headers = bearer(make_token(exp=int(time.time()) - 600))
    response = await http.get("/_test/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert response.headers["WWW-Authenticate"] == challenge


async def test_a_rejected_token_is_a_security_event_without_the_token(
    client: tuple[httpx.AsyncClient, FakeKeys],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "gize_api.core.logging.sentry_sdk.capture_message", lambda event, **_: sent.append(event)
    )
    http, _ = client
    token = make_token(aud="anon")
    for _ in range(3):
        await http.get("/_test/me", headers=bearer(token))
    output = capsys.readouterr().err
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    events = [line for line in lines if line["event"] == "token_rejected"]
    assert [event["reason"] for event in events] == ["audience"] * 3
    assert all(event["level"] == "warning" for event in events)
    assert token.split(".")[2] not in output  # never the token
    assert sent == ["token_rejected"]  # Sentry once a minute per reason


async def test_unreachable_keys_are_503(client: tuple[httpx.AsyncClient, FakeKeys]) -> None:
    http, keys = client
    keys.failing = True
    response = await http.get("/_test/me", headers=bearer(make_token()))
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["Retry-After"]


async def test_a_flood_of_bad_tokens_is_throttled_per_ip(
    client: tuple[httpx.AsyncClient, FakeKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gize_api.core.ratelimit import Limit, Policy

    monkeypatch.setattr(
        "gize_api.core.security.TOKEN_FAILURES",
        Policy("test_token_failures", (Limit("3/minute", "ip"),), fail_open=True),
    )
    http, _ = client
    statuses = [
        (await http.get("/_test/me", headers=bearer("not.a.token"))).status_code for _ in range(5)
    ]
    assert statuses == [401, 401, 401, 429, 429]
    good = await http.get("/_test/me", headers=bearer(make_token()))
    assert good.status_code == 200  # valid tokens are never counted


async def test_the_bearer_scheme_is_in_the_api_schema(make_settings: SettingsFactory) -> None:
    schema = _app(make_settings).openapi()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda request: httpx.Response(200, content=b"<html>not json</html>"),
    ],
)
async def test_a_failing_or_garbled_key_endpoint_is_unavailable(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    monkeypatch.setattr("gize_api.core.http.RETRY_BASE_DELAY_SECONDS", 0.001)
    client = OutboundClient(
        allowed_hosts={make_settings().supabase_host},
        resolver=_public_resolver,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    verifier = TokenVerifier.for_settings(make_settings(), telemetry=None, outbound=client)
    try:
        with pytest.raises(KeysUnavailableError):
            await verifier.verify(make_token())
    finally:
        await verifier.aclose()


async def test_a_failing_verifier_close_does_not_stop_shutdown(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failing(self: TokenVerifier) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(TokenVerifier, "aclose", failing)
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        pass
    assert '"token_verifier_close_failed"' in capsys.readouterr().err


async def test_an_outage_never_queues_requests_behind_failed_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale keys are served at once while one refresh runs in the background (review:
    each request used to wait for its own failed fetch, N requests taking N times 10 s)."""
    keys = FakeKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)  # now stale
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    keys.failing, keys.delay = True, 1.0
    started = time.monotonic()
    results = await asyncio.gather(*(verifier.verify(make_token()) for _ in range(10)))
    assert time.monotonic() - started < 0.5
    assert {result.subject for result in results} == {uuid.UUID(int=7)}
    assert keys.fetches == 2  # the first fetch, plus one shared background refresh
    await verifier.aclose()  # cancels the refresh still running


async def test_a_cold_start_during_an_outage_makes_one_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = FakeKeys(delay=0.5)
    keys.failing = True
    verifier = fake_verifier(keys)
    started = time.monotonic()
    outcomes = await asyncio.gather(
        *(verifier.verify(make_token()) for _ in range(10)), return_exceptions=True
    )
    assert time.monotonic() - started < 1.0  # one shared fetch, not ten in a row
    assert all(isinstance(outcome, KeysUnavailableError) for outcome in outcomes)
    assert keys.fetches == 1
    started = time.monotonic()
    with pytest.raises(KeysUnavailableError):  # and no new fetch inside the interval
        await verifier.verify(make_token())
    assert time.monotonic() - started < 0.1
    assert keys.fetches == 1


async def test_a_duplicated_key_id_keeps_the_first_key() -> None:
    impostor = make_jwk(OTHER_EC_KEY, "ec-1", "ES256")
    verifier = fake_verifier(FakeKeys({"keys": [*KEY_SET["keys"], impostor]}))
    assert (await verifier.verify(make_token())).subject == uuid.UUID(int=7)
    with pytest.raises(InvalidTokenError):
        await verifier.verify(make_token(OTHER_EC_KEY))


async def test_expired_and_missing_tokens_do_not_count_as_attacks(
    client: tuple[httpx.AsyncClient, FakeKeys], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tab in a shared office must not get every colleague a 429 (review)."""
    from gize_api.core.ratelimit import Limit, Policy

    monkeypatch.setattr(
        "gize_api.core.security.TOKEN_FAILURES",
        Policy("test_token_failures", (Limit("2/minute", "ip"),), fail_open=True),
    )
    http, _ = client
    expired = bearer(make_token(exp=int(time.time()) - 600))
    statuses = [(await http.get("/_test/me", headers=expired)).status_code for _ in range(4)]
    statuses += [(await http.get("/_test/me")).status_code for _ in range(4)]
    assert statuses == [401] * 8


class BrokenKeys(FakeKeys):
    """A fetch failing in a way the client does not map, e.g. a deeply nested JSON body."""

    async def __call__(self) -> Any:  # noqa: ANN401 - JSON
        self.fetches += 1
        if self.failing:
            raise RecursionError("maximum recursion depth exceeded")
        return self.key_set


async def test_an_unexpected_fetch_error_is_unavailable_and_logged() -> None:
    keys = BrokenKeys()
    keys.failing = True
    with structlog.testing.capture_logs() as logs, pytest.raises(KeysUnavailableError):
        await fake_verifier(keys).verify(make_token())
    assert [log["event"] for log in logs] == ["signing_keys_refresh_failed"]


async def test_an_unexpected_error_in_a_background_refresh_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = BrokenKeys()
    verifier = fake_verifier(keys)
    await verifier.verify(make_token())
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)  # now stale
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    keys.failing = True
    with structlog.testing.capture_logs() as logs:
        assert (await verifier.verify(make_token())).subject == uuid.UUID(int=7)  # cached key
        await asyncio.sleep(0.01)  # let the background refresh fail
    assert [log["event"] for log in logs] == ["signing_keys_refresh_failed"]
    assert keys.fetches == 2


async def test_shutdown_during_a_refresh_answers_unavailable() -> None:
    """Requests waiting on a refresh that shutdown cancels get a 503, not a cancellation."""
    keys = FakeKeys(delay=10.0)
    verifier = fake_verifier(keys)
    waiting = asyncio.create_task(verifier.verify(make_token()))
    await asyncio.sleep(0.01)  # the request is now waiting on the refresh
    await verifier.aclose()
    with pytest.raises(KeysUnavailableError):
        await waiting


async def test_a_cancelled_request_waiting_on_a_refresh_still_ends() -> None:
    keys = FakeKeys(delay=10.0)
    verifier = fake_verifier(keys)
    waiting = asyncio.create_task(verifier.verify(make_token()))
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await verifier.aclose()  # the shared refresh outlived the request
