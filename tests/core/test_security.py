"""GZ-010: Supabase access tokens are verified against the project's public keys.

Keys and tokens are generated here at runtime (never real tokens, never committed); the
key set is served by a fake fetcher, or through the real outbound client on a mock
transport.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

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
from tests.support import TEST_SUPABASE_URL, SettingsFactory

ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
EC_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _jwk(key: Any, kid: str, alg: str) -> dict[str, Any]:  # noqa: ANN401 - a private key
    algorithm = ECAlgorithm if alg == "ES256" else RSAAlgorithm
    public = json.loads(algorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "alg": alg, "use": "sig"}


KEY_SET = {"keys": [_jwk(EC_KEY, "ec-1", "ES256"), _jwk(RSA_KEY, "rsa-1", "RS256")]}


def _claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": str(uuid.UUID(int=7)),
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        "session_id": str(uuid.UUID(int=9)),
        "email": "someone@example.com",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _token(key: Any = EC_KEY, kid: str = "ec-1", alg: str = "ES256", **claims: Any) -> str:  # noqa: ANN401
    return jwt.encode(_claims(**claims), key, algorithm=alg, headers={"kid": kid})


class FakeKeys:
    """Serves a key set and counts fetches; can fail on demand."""

    def __init__(self, key_set: dict[str, Any] | None = None) -> None:
        self.key_set = key_set if key_set is not None else KEY_SET
        self.fetches = 0
        self.failing = False

    async def __call__(self) -> Any:  # noqa: ANN401 - JSON
        self.fetches += 1
        if self.failing:
            raise KeysUnavailableError("down")
        return self.key_set


def _verifier(keys: FakeKeys | None = None) -> TokenVerifier:
    return TokenVerifier(issuer=ISSUER, fetch_keys=keys or FakeKeys())


async def test_a_valid_es256_token() -> None:
    result = await _verifier().verify(_token())
    assert result == VerifiedToken(
        subject=uuid.UUID(int=7),
        session_id=str(uuid.UUID(int=9)),
        expires_at=result.expires_at,
    )


async def test_a_valid_rs256_token() -> None:
    assert (await _verifier().verify(_token(RSA_KEY, "rsa-1", "RS256"))).subject == uuid.UUID(int=7)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (lambda: _token(exp=int(time.time()) - 120), "expired"),
        (lambda: _token(nbf=int(time.time()) + 600), "not_yet_valid"),
        (lambda: _token(aud="anon"), "audience"),
        (lambda: _token(iss="https://other.supabase.co/auth/v1"), "issuer"),
        (lambda: _token(OTHER_EC_KEY), "signature"),  # signed by a key we don't trust
        (lambda: _token(sub=None), "claims"),
        (lambda: _token(exp=None), "claims"),
        (lambda: _token(sub="not-a-uuid"), "claims"),
        (lambda: _token(role="service_role"), "claims"),
        (lambda: _token(role="anon"), "claims"),
        (lambda: _token(RSA_KEY, "ec-1", "RS256"), "algorithm"),  # alg differs from the key's
        (
            lambda: jwt.encode(
                _claims(),
                "a-guessed-shared-secret-long-enough-for-hs256",
                algorithm="HS256",
                headers={"kid": "ec-1"},
            ),
            "algorithm",
        ),
        (lambda: jwt.encode(_claims(), "", algorithm="none"), "algorithm"),
        (lambda: "not.a.token", "malformed"),
        (lambda: _token(iat="soon"), "malformed"),
        (lambda: jwt.encode(_claims(), EC_KEY, algorithm="ES256"), "unknown_key"),  # no kid
        (lambda: "x" * 9000, "malformed"),
        (lambda: _token(kid="nobody"), "unknown_key"),
    ],
)
async def test_bad_tokens_are_rejected(token: Callable[[], str], reason: str) -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        await _verifier().verify(token())
    assert exc_info.value.reason == reason


async def test_tampering_with_the_payload_breaks_the_signature() -> None:
    header, _payload, signature = _token().split(".")
    forged = jwt.utils.base64url_encode(
        json.dumps({**_claims(), "sub": str(uuid.UUID(int=8))}).encode()
    ).decode()
    with pytest.raises(InvalidTokenError) as exc_info:
        await _verifier().verify(f"{header}.{forged}.{signature}")
    assert exc_info.value.reason == "signature"


async def test_a_small_clock_difference_is_tolerated() -> None:
    assert (await _verifier().verify(_token(exp=int(time.time()) - 10))).subject == uuid.UUID(int=7)


async def test_keys_are_fetched_once_and_cached() -> None:
    keys = FakeKeys()
    verifier = _verifier(keys)
    for _ in range(5):
        await verifier.verify(_token())
    assert keys.fetches == 1


async def test_unknown_keys_cannot_trigger_more_than_one_fetch_per_interval() -> None:
    """Forged key IDs must not turn into a flood of requests to Supabase."""
    keys = FakeKeys()
    verifier = _verifier(keys)
    await verifier.verify(_token())
    for kid in ("forged-1", "forged-2", "forged-3"):
        with pytest.raises(InvalidTokenError) as exc_info:
            await verifier.verify(_token(kid=kid))
        assert exc_info.value.reason == "unknown_key"
    assert keys.fetches == 1  # the last fetch is under REFRESH_MIN_INTERVAL_SECONDS old


async def test_an_unknown_key_refreshes_once_the_interval_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = FakeKeys()
    verifier = _verifier(keys)
    await verifier.verify(_token())
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    with pytest.raises(InvalidTokenError):
        await verifier.verify(_token(kid="forged"))
    assert keys.fetches == 2


async def test_a_rotated_key_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security.REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    keys = FakeKeys()
    verifier = _verifier(keys)
    await verifier.verify(_token())
    new_key = ec.generate_private_key(ec.SECP256R1())
    keys.key_set = {"keys": [*KEY_SET["keys"], _jwk(new_key, "ec-2", "ES256")]}
    assert (await verifier.verify(_token(new_key, "ec-2"))).subject == uuid.UUID(int=7)


async def test_cached_keys_keep_working_while_supabase_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)  # always due a refresh
    keys = FakeKeys()
    verifier = _verifier(keys)
    await verifier.verify(_token())
    keys.failing = True
    assert (await verifier.verify(_token())).subject == uuid.UUID(int=7)


async def test_cached_keys_expire_after_a_long_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gize_api.core.security.KEYS_TTL_SECONDS", 0.0)
    keys = FakeKeys()
    verifier = _verifier(keys)
    await verifier.verify(_token())
    keys.failing = True
    monkeypatch.setattr("gize_api.core.security.KEYS_MAX_STALE_SECONDS", 0.0)
    with pytest.raises(KeysUnavailableError):
        await verifier.verify(_token())


async def test_no_keys_at_all_is_unavailable_not_a_bad_token() -> None:
    keys = FakeKeys()
    keys.failing = True
    with pytest.raises(KeysUnavailableError):
        await _verifier(keys).verify(_token())


@pytest.mark.parametrize(
    "key_set",
    [
        {"keys": []},  # a project still on the shared secret publishes no public keys
        {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "ec-1", "alg": "HS256"}]},
        {"not": "a key set"},
        ["not", "an", "object"],
    ],
)
async def test_unusable_key_sets_are_unavailable(key_set: Any) -> None:  # noqa: ANN401
    with pytest.raises(KeysUnavailableError):
        await _verifier(FakeKeys(key_set)).verify(_token())


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
        assert (await verifier.verify(_token())).subject == uuid.UUID(int=7)
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
            await verifier.verify(_token())
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
        app.state.token_verifier = _verifier(keys)
        await app.state.rate_limiter.aclose()
        app.state.rate_limiter = RateLimiter.for_settings(
            make_settings(redis_url=test_redis_url), telemetry=None, key_prefix=prefix
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, keys


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_valid_token_reaches_the_handler(
    client: tuple[httpx.AsyncClient, FakeKeys],
) -> None:
    http, _ = client
    response = await http.get("/_test/me", headers=_bearer(_token()))
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
        headers = _bearer(_token(exp=int(time.time()) - 600))
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
    token = _token(aud="anon")
    for _ in range(3):
        await http.get("/_test/me", headers=_bearer(token))
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
    response = await http.get("/_test/me", headers=_bearer(_token()))
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
        (await http.get("/_test/me", headers=_bearer("not.a.token"))).status_code for _ in range(5)
    ]
    assert statuses == [401, 401, 401, 429, 429]
    good = await http.get("/_test/me", headers=_bearer(_token()))
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
            await verifier.verify(_token())
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
