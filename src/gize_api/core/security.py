"""Who is calling: Supabase access tokens, verified against the project's public keys.

- **Asymmetric keys only** (owner decision, GZ-010): ES256 or RS256, from the project's key
  set (JWKS). The API never holds a secret that could mint tokens; `none`, HS256 and any
  algorithm that does not match the key are refused before the signature is checked.
- **Checked:** signature, `exp` and `nbf` (with LEEWAY_SECONDS of clock difference),
  `aud = authenticated`, `iss = <project>/auth/v1`, a UUID `sub`, `role = authenticated`
  (no anon or service_role tokens), not an anonymous sign-in, and the presence of `exp`,
  `iat`, `sub`, `aud`, `iss`. Not checked: `aal` (MFA). A token stays valid after sign-out
  until it expires; GZ-011's active-staff check on every request is the real revocation.
- **Keys** are the key set's signature keys (`use` absent or `sig`, no private material;
  for a duplicated key ID the first wins). They come through the guarded outbound client
  and are cached for KEYS_TTL_SECONDS. Refreshes happen at most every
  REFRESH_MIN_INTERVAL_SECONDS, one at a time, shared by concurrent requests: forged key
  IDs cannot flood Supabase, and an outage never queues requests behind failed fetches.
  Stale keys are served at once while a refresh runs in the background; they keep working
  for up to KEYS_MAX_STALE_SECONDS. With none, requests get 503 at once. A revoked key
  stays trusted until the next successful refresh (restart the instances to drop it).
- **Rejected tokens:** 401 UNAUTHENTICATED with `WWW-Authenticate: Bearer`, a security
  event (the reason only, never the token; Sentry once a minute per reason and process).
  Rejections that look like forgery (not a missing or expired token) count against
  TOKEN_FAILURES per client IP: a flood of forged tokens gets 429.

Who the caller is *in Gize* (GZ-011):

- **`current_staff`**: the token's `sub` must be an active row in `gize.staff`, else 403.
  The role comes from that row, never from the token. It sets the staff ID for logs and
  rate limits, and hands on a `CurrentStaff` (ID and role), not the ORM row (the session
  rule, architecture.md). Users who are not staff at all count against TOKEN_FAILURES
  (Supabase may let anyone sign up); deactivated staff do not (a stale tab).
- **`require_role(...)`**: `current_staff` plus a role check, 403 otherwise.
- **`staff_router(...)`**: an APIRouter whose routes count against the STAFF limit, per
  staff member. Every `/v1` route needs `current_staff` and that limit unless it is listed
  as public (a test enforces it).
- **Object-level checks** belong in services, inside the block that reads or writes the
  object: a missing object and one the caller may not touch give the same 404.
"""

import asyncio
import contextlib
import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Self

import jwt
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.config import Settings
from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.http import OutboundClient, OutboundError
from gize_api.core.logging import log_security_event, set_staff_id
from gize_api.core.ratelimit import STAFF, Limit, Policy, RateLimiter, client_ip, rate_limited
from gize_api.core.telemetry import Telemetry
from gize_api.staff.models import StaffRole
from gize_api.staff.repository import get_staff

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
# Missing, expired or not-yet-valid tokens are ordinary (a stale tab), never counted.
SUSPICIOUS_REASONS = frozenset(
    {"malformed", "algorithm", "unknown_key", "signature", "audience", "issuer", "claims"}
)

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
        self._refreshing: asyncio.Task[None] | None = None

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
        if self._refreshing is not None and not self._refreshing.done():
            self._refreshing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refreshing
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
        except jwt.PyJWTError:  # e.g. a non-numeric `iat`, or a key PyJWT cannot use
            raise InvalidTokenError("malformed") from None
        return _verified(claims)

    async def _key(self, kid: str) -> jwt.PyJWK:
        if kid in self._keys and self._fresh():
            return self._keys[kid]
        if self._due():
            refresh = self._start_refresh()
            # Wait only when the answer depends on it: no usable keys yet, or a key ID
            # we have never seen (perhaps just rotated in). Otherwise serve what we have.
            if not self._usable() or kid not in self._keys:
                try:
                    await asyncio.shield(refresh)
                except asyncio.CancelledError:
                    # Shutdown stopped the refresh: answer from what we have (503 if
                    # nothing). A cancelled request still ends.
                    current = asyncio.current_task()
                    if not refresh.cancelled() or current is None or current.cancelling():
                        raise
        if not self._usable():
            raise KeysUnavailableError("no usable signing keys")
        if kid not in self._keys:
            raise InvalidTokenError("unknown_key")
        return self._keys[kid]

    def _fresh(self) -> bool:
        return self._fetched_at is not None and (
            time.monotonic() - self._fetched_at < KEYS_TTL_SECONDS
        )

    def _usable(self) -> bool:
        return self._fetched_at is not None and (
            time.monotonic() - self._fetched_at <= KEYS_MAX_STALE_SECONDS
        )

    def _due(self) -> bool:
        """A refresh is running (join it) or the last attempt is old enough to try again."""
        if self._refreshing is not None and not self._refreshing.done():
            return True
        return self._attempted_at is None or (
            time.monotonic() - self._attempted_at >= REFRESH_MIN_INTERVAL_SECONDS
        )

    def _start_refresh(self) -> "asyncio.Task[None]":
        if self._refreshing is None or self._refreshing.done():
            self._attempted_at = time.monotonic()
            # A clean context: the fetch outlives the request that started it.
            self._refreshing = asyncio.create_task(self._refresh(), context=contextvars.Context())
        return self._refreshing

    async def _refresh(self) -> None:
        try:
            keys = _usable_keys(await self._fetch_keys())
        except Exception as exc:  # any failure leaves the cache as it was; logged
            logger.warning("signing_keys_refresh_failed", exc_info=exc)
            return
        self._keys, self._fetched_at = keys, time.monotonic()


def _usable_keys(key_set: Any) -> dict[str, jwt.PyJWK]:  # noqa: ANN401 - JSON
    """The set's public signature keys with a key ID and a supported algorithm."""
    members = key_set.get("keys") if isinstance(key_set, Mapping) else None
    if not isinstance(members, list):
        raise KeysUnavailableError("the key set is not a key set")
    keys: dict[str, jwt.PyJWK] = {}
    for member in members:
        if not _signature_key(member):
            continue
        try:
            key = jwt.PyJWK(member)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
        if not key.key_id or key.algorithm_name not in SUPPORTED_ALGORITHMS:
            continue
        if key.key_id in keys:
            logger.warning("signing_key_id_duplicated")  # the first one stays trusted
            continue
        keys[key.key_id] = key
    if not keys:  # e.g. a project still on the shared secret publishes no public keys
        raise KeysUnavailableError("the key set holds no asymmetric signing key")
    return keys


def _signature_key(member: Any) -> bool:  # noqa: ANN401 - JSON
    if not isinstance(member, Mapping) or "d" in member:  # private material: never trusted
        return False
    if member.get("use") not in (None, "sig"):
        return False
    operations = member.get("key_ops")
    return operations is None or (isinstance(operations, list) and "verify" in operations)


def _verified(claims: Mapping[str, Any]) -> VerifiedToken:
    if claims.get("role") != AUDIENCE:  # an anon or service_role token
        raise InvalidTokenError("claims")
    if claims.get("is_anonymous") is True:  # an anonymous sign-in is no one
        raise InvalidTokenError("claims")
    try:
        subject = uuid.UUID(str(claims["sub"]))
    except ValueError:
        raise InvalidTokenError("claims") from None
    session_id = claims.get("session_id")
    try:
        expires_at = datetime.fromtimestamp(int(claims["exp"]), UTC)
    except (OverflowError, ValueError, OSError):
        raise InvalidTokenError("claims") from None
    return VerifiedToken(
        subject=subject,
        session_id=session_id if isinstance(session_id, str) else None,
        expires_at=expires_at,
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
    if reason in SUSPICIOUS_REASONS:
        await _count_failure(request)
    log_security_event(
        "token_rejected",
        sentry=RateLimiter._due(_reported_at, reason, SENTRY_INTERVAL_SECONDS),
        reason=reason,
    )
    challenge = "Bearer" if reason == "missing" else 'Bearer error="invalid_token"'
    raise AppError(ErrorCode.UNAUTHENTICATED, headers={"WWW-Authenticate": challenge})


async def _count_failure(request: Request) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    settings: Settings = request.app.state.settings
    await limiter.check(TOKEN_FAILURES, ip=client_ip(request, settings.client_ip_header))


@dataclass(frozen=True)
class CurrentStaff:
    """The signed-in staff member, as read at the start of this request."""

    id: uuid.UUID
    role: StaffRole


async def current_staff(
    request: Request,
    token: VerifiedToken = Depends(verified_token),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CurrentStaff:
    """FastAPI dependency: the caller as active staff, or 401/403/429/503."""
    async with session.begin():
        staff = await get_staff(session, token.subject)
        found = None if staff is None else (staff.is_active, CurrentStaff(staff.id, staff.role))
    if found is None:
        await _count_failure(request)  # a valid token, but not ours: maybe open sign-ups
        _denied("staff_access_denied", reason="not_staff", user_id=str(token.subject))
    is_active, caller = found
    if not is_active:
        _denied("staff_access_denied", reason="inactive", user_id=str(token.subject))
    set_staff_id(str(caller.id))
    return caller


def require_role(*roles: StaffRole) -> Callable[..., Awaitable[CurrentStaff]]:
    """FastAPI dependency: `current_staff` in one of `roles`, or 403."""
    if not roles:
        raise ValueError("require_role needs at least one role")
    allowed = frozenset(roles)

    async def dependency(caller: CurrentStaff = Depends(current_staff)) -> CurrentStaff:  # noqa: B008
        if caller.role not in allowed:
            _denied(
                "permission_denied",
                reason="role",
                role=caller.role.value,
                required_roles=sorted(allowed),
            )
        return caller

    return dependency


# Counted after `current_staff`, so per staff member, not per office IP.
staff_rate_limit = rate_limited(STAFF, after=current_staff)


def staff_router(**options: Any) -> APIRouter:  # noqa: ANN401 - APIRouter's options
    """An APIRouter for staff routes: each one counts against the STAFF limit."""
    dependencies = [Depends(staff_rate_limit), *options.pop("dependencies", ())]
    return APIRouter(dependencies=dependencies, **options)


def _denied(event: str, *, reason: str, **fields: Any) -> NoReturn:  # noqa: ANN401
    log_security_event(
        event,
        sentry=RateLimiter._due(_reported_at, f"{event}:{reason}", SENTRY_INTERVAL_SECONDS),
        reason=reason,
        **fields,
    )
    raise AppError(ErrorCode.FORBIDDEN)
