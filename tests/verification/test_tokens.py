"""GZ-031: the verified-phone token and the code hash.

Only a token we signed, for this audience, unexpired and complete, is accepted; one
setting yields two independent keys (code hashes and tokens).
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from gize_api.verification.tokens import (
    AUDIENCE,
    ISSUER,
    InvalidVerifiedPhoneTokenError,
    _token_key,
    code_hash,
    issue_verified_phone_token,
    read_verified_phone_token,
)
from tests.verification.support import TOKEN_KEY

PHONE = "+251911234567"
VERIFICATION = uuid.UUID(int=7)
OTHER_KEY = "another-signing-key-0123456789abcdef"


def test_a_token_reads_back_what_it_proves() -> None:
    token, expires = issue_verified_phone_token(PHONE, VERIFICATION, key=TOKEN_KEY)
    proved = read_verified_phone_token(token, key=TOKEN_KEY)
    assert (proved.phone, proved.verification_id) == (PHONE, VERIFICATION)
    assert proved.expires_at == expires.replace(microsecond=0)


def _forged(**changes: object) -> str:
    """A token like ours, signed with our real token key, with some claims changed (None
    removes one)."""
    token, _ = issue_verified_phone_token(PHONE, VERIFICATION, key=TOKEN_KEY)
    claims = jwt.decode(token, options={"verify_signature": False})
    claims.update(changes)
    kept = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(kept, _token_key(TOKEN_KEY), algorithm="HS256")


def _expired() -> str:
    issued = datetime.now(UTC) - timedelta(minutes=31)
    return issue_verified_phone_token(PHONE, VERIFICATION, key=TOKEN_KEY, now=issued)[0]


def _raw_key() -> str:
    """Signed with the setting itself instead of the derived token key."""
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": PHONE, "jti": str(VERIFICATION),
        "iat": now, "exp": now + timedelta(minutes=5),
    }  # fmt: skip
    return jwt.encode(claims, TOKEN_KEY, algorithm="HS256")


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: _forged(aud="gize-staff"), id="another audience"),
        pytest.param(lambda: _forged(iss="supabase"), id="another issuer"),
        pytest.param(lambda: _forged(sub=None), id="no phone"),
        pytest.param(lambda: _forged(jti=None), id="no verification"),
        pytest.param(lambda: _forged(jti="not-a-uuid"), id="a bad verification"),
        pytest.param(_expired, id="expired"),
        pytest.param(
            lambda: issue_verified_phone_token(PHONE, VERIFICATION, key=OTHER_KEY)[0],
            id="another key",
        ),
        pytest.param(_raw_key, id="the raw setting as key"),
        pytest.param(lambda: "not.a.token", id="garbage"),
    ],
)
def test_only_our_own_valid_tokens_are_accepted(make: Callable[[], str]) -> None:
    with pytest.raises(InvalidVerifiedPhoneTokenError):
        read_verified_phone_token(make(), key=TOKEN_KEY)


def test_an_unsigned_token_is_refused() -> None:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": PHONE, "jti": str(VERIFICATION),
        "iat": now, "exp": now + timedelta(minutes=5),
    }  # fmt: skip
    unsigned = jwt.encode(claims, key="", algorithm="none")
    with pytest.raises(InvalidVerifiedPhoneTokenError):
        read_verified_phone_token(unsigned, key=TOKEN_KEY)


def test_a_code_hash_depends_on_the_verification_the_code_and_the_key() -> None:
    base = code_hash(VERIFICATION, "123456", key=TOKEN_KEY)
    assert len(base) == 64
    assert base == code_hash(VERIFICATION, "123456", key=TOKEN_KEY)
    assert base != code_hash(uuid.UUID(int=8), "123456", key=TOKEN_KEY)
    assert base != code_hash(VERIFICATION, "123457", key=TOKEN_KEY)
    assert base != code_hash(VERIFICATION, "123456", key=OTHER_KEY)
