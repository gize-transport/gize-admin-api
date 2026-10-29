"""Who is calling: Supabase access tokens, verified against the project's public keys.

- **Asymmetric keys only** (owner decision, GZ-010): ES256 or RS256, from the project's key
  set (JWKS). The API never holds a secret that could mint tokens; `none`, HS256 and any
  algorithm that does not match the key are refused before the signature is checked.
- **Checked:** signature, `exp` and `nbf` (with LEEWAY_SECONDS of clock difference),
  `aud = authenticated`, `iss = <project>/auth/v1`, a UUID `sub`, `role = authenticated`
  (no anon or service_role tokens), and the presence of `exp`, `iat`, `sub`, `aud`, `iss`.
- **Keys** come through the guarded outbound client and are cached for KEYS_TTL_SECONDS.
  An unknown key ID triggers one refresh, at most every REFRESH_MIN_INTERVAL_SECONDS, so
  forged key IDs cannot flood Supabase. While Supabase is unreachable, cached keys keep
  working for up to KEYS_MAX_STALE_SECONDS; with none, requests get 503.
- **Rejected tokens:** 401 UNAUTHENTICATED with `WWW-Authenticate: Bearer`, a security
  event (the reason only, never the token; Sentry once a minute per reason and process),
  and a count against TOKEN_FAILURES per client IP: a flood of bad tokens gets 429.
- Who the caller is *in Gize* (staff row, role) is GZ-011's `current_staff`, built on
  `verified_token`.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

import jwt
import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gize_api.core.config import Settings
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.http import OutboundClient, OutboundError
from gize_api.core.logging import log_security_event
from gize_api.core.ratelimit import Limit, Policy, RateLimiter, client_ip
from gize_api.core.telemetry import Telemetry

SUPPORTED_ALGORITHMS = frozenset({"ES256", "RS256"})
AUDIENCE = "authenticated"
REQUIRED_CLAIMS = ("exp", "iat", "sub", "aud", "iss")
LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 8192
KEYS_TTL_SECONDS = 600.0
KEYS_MAX_STALE_SECONDS = 24 * 3600.0
REFRESH_MIN_INTERVAL_SECONDS = 30.0
SENTRY_INTERVAL_SECONDS = 60.0
UNAVAILABLE_RETRY_AFTER_SECONDS = "5"
# Counted only when a token is rejected: valid callers never use it up.
TOKEN_FAILURES = Policy("token_failures", (Limit("30/minute", "ip"),), fail_open=True)

KeyFetcher = Callable[[], Awaitable[Any]]

logger = structlog.get_logger("gize_api.security")
_bearer = HTTPBearer(auto_error=False, description="A Supabase access token")
_reported_at: dict[str, float] = {}


class InvalidTokenError(Exception):
    """The token is not acceptable. `reason` is a fixed word, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeysUnavailableError(Exception):
    """The project's signing keys could not be obtained (and none usable are cached)."""


@dataclass(frozen=True)
class VerifiedToken:
    subject: uuid.UUID  # the Supabase user ID
    session_id: str | None
    expires_at: datetime


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        fetch_keys: KeyFetcher,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._issuer = issuer
        self._fetch_keys = fetch_keys
        self._close = close
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        telemetry: Telemetry | None,
        outbound: OutboundClient | None = None,
    ) -> Self:
        client = outbound or OutboundClient(
            allowed_hosts={settings.supabase_host}, telemetry=telemetry
        )

        async def fetch() -> Any:  # noqa: ANN401 - JSON
            try:
                response = await client.request("GET", settings.supabase_jwks_url)
            except OutboundError:
                raise KeysUnavailableError("the key set could not be fetched") from None
            if response.status != 200:
                raise KeysUnavailableError("the key set request failed")
            try:
                return response.json()
            except ValueError:
                raise KeysUnavailableError("the key set is not JSON") from None

        return cls(issuer=settings.supabase_auth_issuer, fetch_keys=fetch, close=client.aclose)

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def verify(self, token: str) -> VerifiedToken:
        if len(token) > MAX_TOKEN_LENGTH:
            raise InvalidTokenError("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise InvalidTokenError("malformed") from None
        algorithm, kid = header.get("alg"), header.get("kid")
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise InvalidTokenError("algorithm")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("unknown_key")
        key = await self._key(kid)
        if key.algorithm_name != algorithm:
            raise InvalidTokenError("algorithm")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError:
            raise InvalidTokenError("expired") from None
        except jwt.ImmatureSignatureError:
            raise InvalidTokenError("not_yet_valid") from None
        except jwt.InvalidAudienceError:
            raise InvalidTokenError("audience") from None
        except jwt.InvalidIssuerError:
            raise InvalidTokenError("issuer") from None
        except jwt.InvalidSignatureError:
            raise InvalidTokenError("signature") from None
        except jwt.MissingRequiredClaimError:
            raise InvalidTokenError("claims") from None
        except jwt.InvalidTokenError:  # e.g. a non-numeric `iat`
            raise InvalidTokenError("malformed") from None
        return _verified(claims)

    async def _key(self, kid: str) -> jwt.PyJWK:
        if kid in self._keys and self._fresh():
            return self._keys[kid]
        async with self._lock:
            if not (kid in self._keys and self._fresh()) and self._may_refresh(kid):
                await self._refresh()
            if self._fetched_at is None or (
                time.monotonic() - self._fetched_at > KEYS_MAX_STALE_SECONDS
            ):
                raise KeysUnavailableError("no usable signing keys")
        if kid not in self._keys:
            raise InvalidTokenError("unknown_key")
        return self._keys[kid]

    def _fresh(self) -> bool:
        return self._fetched_at is not None and (
            time.monotonic() - self._fetched_at < KEYS_TTL_SECONDS
        )

    def _may_refresh(self, kid: str) -> bool:
        """Due when stale; for an unknown key ID, at most once per interval."""
        if not self._fresh():
            return True
        return self._attempted_at is None or (
            kid not in self._keys
            and time.monotonic() - self._attempted_at >= REFRESH_MIN_INTERVAL_SECONDS
        )

    async def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        try:
            keys = _usable_keys(await self._fetch_keys())
        except KeysUnavailableError as exc:
            logger.warning("signing_keys_refresh_failed", exc_info=exc)
            return
        self._keys, self._fetched_at = keys, time.monotonic()


def _usable_keys(key_set: Any) -> dict[str, jwt.PyJWK]:  # noqa: ANN401 - JSON
    """The set's signature keys with a key ID and a supported algorithm."""
    if not isinstance(key_set, Mapping):
        raise KeysUnavailableError("the key set is not an object")
    try:
        parsed = jwt.PyJWKSet.from_dict(dict(key_set))
    except (jwt.PyJWKSetError, jwt.InvalidKeyError, ValueError, TypeError):
        raise KeysUnavailableError("the key set holds no usable key") from None
    keys = {
        key.key_id: key
        for key in parsed.keys
        if key.key_id and key.algorithm_name in SUPPORTED_ALGORITHMS
    }
    if not keys:  # e.g. a project still on the shared secret publishes no public keys
        raise KeysUnavailableError("the key set holds no asymmetric signing key")
    return keys


def _verified(claims: Mapping[str, Any]) -> VerifiedToken:
    if claims.get("role") != AUDIENCE:  # an anon or service_role token
        raise InvalidTokenError("claims")
    try:
        subject = uuid.UUID(str(claims["sub"]))
    except ValueError:
        raise InvalidTokenError("claims") from None
    session_id = claims.get("session_id")
    return VerifiedToken(
        subject=subject,
        session_id=session_id if isinstance(session_id, str) else None,
        expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
    )


async def verified_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> VerifiedToken:
    """FastAPI dependency: the caller's verified Supabase token, or 401/429/503."""
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        if credentials is None or not credentials.credentials:
            raise InvalidTokenError("missing")
        return await verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        await _rejected(request, exc.reason)
        raise  # pragma: no cover - _rejected always raises
    except KeysUnavailableError:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            headers={"Retry-After": UNAVAILABLE_RETRY_AFTER_SECONDS},
        ) from None


async def _rejected(request: Request, reason: str) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    settings: Settings = request.app.state.settings
    await limiter.check(TOKEN_FAILURES, ip=client_ip(request, settings.client_ip_header))
    log_security_event(
        "token_rejected",
        sentry=RateLimiter._due(_reported_at, reason, SENTRY_INTERVAL_SECONDS),
        reason=reason,
    )
    challenge = "Bearer" if reason == "missing" else 'Bearer error="invalid_token"'
    raise AppError(ErrorCode.UNAUTHENTICATED, headers={"WWW-Authenticate": challenge})
