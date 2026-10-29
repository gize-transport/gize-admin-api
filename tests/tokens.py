"""Supabase-style signing keys and access tokens for tests, generated at runtime.

Never real tokens, never committed keys. `FakeKeys` serves the key set to a TokenVerifier.
"""

import asyncio
import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from gize_api.core.security import KeysUnavailableError, TokenVerifier
from tests.support import TEST_SUPABASE_URL

ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
EC_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_EC_KEY = ec.generate_private_key(ec.SECP256R1())


def make_jwk(key: Any, kid: str, alg: str) -> dict[str, Any]:  # noqa: ANN401 - a private key
    algorithm = ECAlgorithm if alg == "ES256" else RSAAlgorithm
    public = json.loads(algorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "alg": alg, "use": "sig"}


KEY_SET = {"keys": [make_jwk(EC_KEY, "ec-1", "ES256"), make_jwk(RSA_KEY, "rsa-1", "RS256")]}


def make_claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Claims like Supabase's; an override of None removes the claim."""
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


def make_token(
    key: Any = EC_KEY,  # noqa: ANN401 - a private key
    kid: str = "ec-1",
    alg: str = "ES256",
    **claims: Any,  # noqa: ANN401
) -> str:
    return jwt.encode(make_claims(**claims), key, algorithm=alg, headers={"kid": kid})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeKeys:
    """Serves a key set and counts fetches; can fail, slowly if asked, like a real outage."""

    def __init__(self, key_set: Any = None, delay: float = 0.0) -> None:  # noqa: ANN401
        self.key_set = key_set if key_set is not None else KEY_SET
        self.fetches = 0
        self.failing = False
        self.delay = delay

    async def __call__(self) -> Any:  # noqa: ANN401 - JSON
        self.fetches += 1
        if self.delay:
            await asyncio.sleep(self.delay)  # a slow fetch is the behaviour under test
        if self.failing:
            raise KeysUnavailableError("down")
        return self.key_set


def fake_verifier(keys: FakeKeys | None = None) -> TokenVerifier:
    return TokenVerifier(issuer=ISSUER, fetch_keys=keys or FakeKeys())
