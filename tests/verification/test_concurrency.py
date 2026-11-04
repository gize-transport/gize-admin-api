"""GZ-031: parallel guesses at one code never get more than five tries.

The check locks the verification row, so concurrent requests take turns: each counts its
attempt before the next reads the count. Committed rows, one connection per request.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.verification.models import PhoneVerification
from gize_api.verification.tokens import code_hash
from tests.verification.support import TOKEN_KEY

VERIFY = "/v1/verification/verify"


async def test_parallel_wrong_guesses_stop_at_five(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verification_id = uuid.uuid4()
    async with committed_sessionmaker() as session, session.begin():
        session.add(
            PhoneVerification(
                id=verification_id,
                phone="+251911234567",
                code_hash=code_hash(verification_id, "123456", key=TOKEN_KEY),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    body = {"verification_id": str(verification_id), "code": "000000"}
    responses = await asyncio.gather(*(committed_api.post(VERIFY, json=body) for _ in range(8)))
    codes = sorted(response.json()["code"] for response in responses)
    assert codes == ["VERIFICATION_CODE_INVALID"] * 5 + ["VERIFICATION_EXPIRED"] * 3
    async with committed_sessionmaker() as session, session.begin():
        row = await session.get_one(PhoneVerification, verification_id)
        assert row.attempts == 5
    right = await committed_api.post(VERIFY, json={**body, "code": "123456"})
    assert right.json()["code"] == "VERIFICATION_EXPIRED"  # too late, however right


async def test_two_right_codes_at_once_get_one_token(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The lock makes the second see the first's `verified_at`: one token per code."""
    verification_id = uuid.uuid4()
    async with committed_sessionmaker() as session, session.begin():
        session.add(
            PhoneVerification(
                id=verification_id,
                phone="+251911234567",
                code_hash=code_hash(verification_id, "123456", key=TOKEN_KEY),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    body = {"verification_id": str(verification_id), "code": "123456"}
    responses = await asyncio.gather(*(committed_api.post(VERIFY, json=body) for _ in range(4)))
    assert sorted(response.status_code for response in responses) == [200, 410, 410, 410]
