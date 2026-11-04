"""The "verified phone" token (GZ-031): proof that the bearer received the code sent to a
phone, for a sign-up (GZ-032) to present.

An HS256 JWT signed with VERIFIED_PHONE_TOKEN_KEY: issuer `gize-api`, audience
`gize-verified-phone`, the phone (`sub`, E.164), the verification (`jti`) and a 30-minute
lifetime. Our own key, never Supabase's: these tokens prove nothing about staff. A sign-up
spends the token once, by its verification (GZ-032).
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ISSUER = "gize-api"
AUDIENCE = "gize-verified-phone"
TOKEN_LIFETIME = timedelta(minutes=30)
_ALGORITHM = "HS256"


class InvalidVerifiedPhoneTokenError(Exception):
    """Not a valid, unexpired token of ours (the reason is never shown to the client)."""


@dataclass(frozen=True)
class VerifiedPhone:
    phone: str
    verification_id: uuid.UUID
    expires_at: datetime


def issue_verified_phone_token(
    phone: str, verification_id: uuid.UUID, *, key: str, now: datetime | None = None
) -> tuple[str, datetime]:
    issued = now or datetime.now(UTC)
    expires = issued + TOKEN_LIFETIME
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": phone,
        "jti": str(verification_id),
        "iat": issued,
        "exp": expires,
    }
    return jwt.encode(claims, _token_key(key), algorithm=_ALGORITHM), expires


def read_verified_phone_token(token: str, *, key: str) -> VerifiedPhone:
    try:
        claims = jwt.decode(
            token,
            _token_key(key),
            algorithms=[_ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["iss", "aud", "sub", "jti", "iat", "exp"]},
        )
        return VerifiedPhone(
            phone=str(claims["sub"]),
            verification_id=uuid.UUID(str(claims["jti"])),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise InvalidVerifiedPhoneTokenError from exc


def code_hash(verification_id: uuid.UUID, code: str, *, key: str) -> str:
    """The stored form of a code: an HMAC under a key of its own, derived from the setting."""
    return hmac.new(
        _derived(key, b"gize-otp-code"), f"{verification_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def _token_key(key: str) -> bytes:
    return _derived(key, b"gize-verified-phone-token")


def _derived(key: str, purpose: bytes) -> bytes:
    """One setting, two independent keys: a code hash can't help forge a token."""
    return hmac.new(key.encode(), purpose, hashlib.sha256).digest()
