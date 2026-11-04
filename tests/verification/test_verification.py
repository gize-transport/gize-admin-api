"""GZ-031: phone verification for the public app, before any sign-up.

`POST /v1/verification/request` sends a 6-digit code by SMS (stored as a keyed hash, valid
10 minutes); `POST /v1/verification/verify` checks it (5 attempts) and returns a signed
"verified phone" token, valid 30 minutes, that a sign-up presents (GZ-032).
Both are public and rate limited, failing closed: each SMS costs money.
"""

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.idempotency import IdempotencyRecord
from gize_api.integrations.sms import FakeSmsSender, SmsSendError
from gize_api.main import GizeAPI
from gize_api.sms.models import SmsMessage, SmsStatus
from gize_api.verification.models import PhoneVerification
from gize_api.verification.tokens import read_verified_phone_token
from tests.verification.support import TOKEN_KEY, broken_redis

REQUEST, VERIFY = "/v1/verification/request", "/v1/verification/verify"
PHONE = "+251911234567"


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _code(sender: FakeSmsSender) -> str:
    _, text = sender.sent[-1]
    match = re.search(r"\b(\d{6})\b", text)
    assert match is not None, text
    return match.group(1)


async def _requested(api: httpx.AsyncClient, phone: str = "0911 23 45 67") -> str:
    response = await api.post(REQUEST, json={"phone": phone}, headers=_key())
    assert response.status_code == 201, response.json()
    return str(response.json()["verification_id"])


async def _row(session: AsyncSession, verification_id: str) -> PhoneVerification:
    async with session.begin():
        return await session.get_one(PhoneVerification, uuid.UUID(verification_id))


# Requesting a code


async def test_a_code_is_sent_and_only_its_hash_is_kept(
    api: httpx.AsyncClient, db_session: AsyncSession, sms: FakeSmsSender
) -> None:
    response = await api.post(REQUEST, json={"phone": "0911 23 45 67"}, headers=_key())
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"verification_id", "expires_at"}
    ((to, text),) = sms.sent
    code = _code(sms)
    assert to == PHONE  # stored and sent in E.164, however it was typed
    assert text == f"Gize: your code is {code}. It expires in 10 minutes."
    row = await _row(db_session, body["verification_id"])
    assert (row.phone, row.attempts, row.verified_at) == (PHONE, 0, None)
    assert code not in row.code_hash
    assert len(row.code_hash) == 64
    assert row.expires_at - row.created_at == timedelta(minutes=10)
    async with db_session.begin():
        (message,) = (await db_session.scalars(select(SmsMessage))).all()
    assert (message.dedup_key, message.status) == (f"otp:{row.id}", SmsStatus.SENT)


async def test_a_retried_request_replays_without_a_second_sms(
    api: httpx.AsyncClient, sms: FakeSmsSender
) -> None:
    key = _key()
    first = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    again = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert len(sms.sent) == 1


async def test_an_idempotency_key_is_required(api: httpx.AsyncClient, sms: FakeSmsSender) -> None:
    response = await api.post(REQUEST, json={"phone": PHONE})
    assert (response.status_code, response.json()["code"]) == (428, "IDEMPOTENCY_KEY_REQUIRED")
    assert sms.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"phone": None},
        {"phone": "0811234567"},  # not a mobile
        {"phone": "+15551234567"},
        {"phone": "091123456"},
        {"phone": 911234567},
        {"phone": PHONE, "name": "Abebe"},
        [],
    ],
    ids=str,
)
async def test_a_bad_request_body_is_422_and_sends_nothing(
    api: httpx.AsyncClient, sms: FakeSmsSender, body: object
) -> None:
    response = await api.post(REQUEST, json=body, headers=_key())
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")
    assert sms.sent == []


async def test_three_codes_an_hour_per_phone(api: httpx.AsyncClient, sms: FakeSmsSender) -> None:
    for _ in range(3):
        await _requested(api, PHONE)
    response = await api.post(REQUEST, json={"phone": "0911234567"}, headers=_key())
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")
    assert "Retry-After" in response.headers
    assert len(sms.sent) == 3
    other = await api.post(REQUEST, json={"phone": "+251922334455"}, headers=_key())
    assert other.status_code == 201  # another phone, the same caller


async def test_ten_codes_an_hour_per_caller(api: httpx.AsyncClient, sms: FakeSmsSender) -> None:
    for n in range(10):
        await _requested(api, f"+2519110000{n:02d}")
    response = await api.post(REQUEST, json={"phone": "+251911000099"}, headers=_key())
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")
    assert len(sms.sent) == 10


async def test_without_redis_no_code_is_sent(
    api: httpx.AsyncClient, api_app: GizeAPI, sms: FakeSmsSender
) -> None:
    """Fails closed: without the limits, anyone could send SMS at our cost."""
    async with broken_redis(api_app):
        response = await api.post(REQUEST, json={"phone": PHONE}, headers=_key())
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
    assert sms.sent == []


@pytest.mark.parametrize("kind", ["unavailable", "rejected", "timeout"])
async def test_an_sms_that_fails_can_be_retried_with_the_same_key(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    kind: str,
) -> None:
    """The key is released and the code expired: the app's retry sends a new code."""

    class Failing:
        async def send(self, to: str, text: str) -> str:
            raise SmsSendError(kind)  # type: ignore[arg-type]

    api_app.state.sms_sender = Failing()
    key = _key()
    response = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    assert (response.status_code, response.json()["code"]) == (503, "SMS_UNAVAILABLE")
    assert "Retry-After" in response.headers
    async with db_session.begin():
        (failed,) = (await db_session.scalars(select(PhoneVerification))).all()
        assert failed.expires_at <= failed.created_at  # unusable
        assert (await db_session.scalars(select(IdempotencyRecord))).all() == []
    api_app.state.sms_sender = sms
    again = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    assert again.status_code == 201
    assert again.json()["verification_id"] != str(failed.id)
    assert len(sms.sent) == 1


async def test_without_the_signing_key_nothing_is_sent(
    api: httpx.AsyncClient, api_app: GizeAPI, sms: FakeSmsSender
) -> None:
    """VERIFIED_PHONE_TOKEN_KEY unset: no code is worth sending if it can't be confirmed."""
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"verified_phone_token_key": None}
    )
    for path, body in (
        (REQUEST, {"phone": PHONE}),
        (VERIFY, {"verification_id": str(uuid.uuid4()), "code": "123456"}),
    ):
        response = await api.post(path, json=body, headers=_key())
        assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
    assert sms.sent == []


async def test_the_database_down_sends_nothing(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    sms: FakeSmsSender,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.post(REQUEST, json={"phone": PHONE}, headers=_key())
    assert response.status_code == 503
    assert sms.sent == []


# Checking a code


async def test_the_right_code_returns_a_verified_phone_token(
    api: httpx.AsyncClient, db_session: AsyncSession, sms: FakeSmsSender
) -> None:
    verification_id = await _requested(api)
    response = await api.post(VERIFY, json={"verification_id": verification_id, "code": _code(sms)})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"verified_phone_token", "expires_at"}
    claims = read_verified_phone_token(body["verified_phone_token"], key=TOKEN_KEY)
    assert (claims.phone, str(claims.verification_id)) == (PHONE, verification_id)
    lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
    assert timedelta(minutes=29) < lifetime <= timedelta(minutes=30)
    assert (await _row(db_session, verification_id)).verified_at is not None


async def test_a_wrong_code_counts_an_attempt(
    api: httpx.AsyncClient, db_session: AsyncSession, sms: FakeSmsSender
) -> None:
    verification_id = await _requested(api)
    wrong = "000000" if _code(sms) != "000000" else "111111"
    response = await api.post(VERIFY, json={"verification_id": verification_id, "code": wrong})
    assert (response.status_code, response.json()["code"]) == (422, "VERIFICATION_CODE_INVALID")
    assert (await _row(db_session, verification_id)).attempts == 1
    right = await api.post(VERIFY, json={"verification_id": verification_id, "code": _code(sms)})
    assert right.status_code == 200  # within the five attempts


async def test_after_five_wrong_codes_the_code_is_gone(
    api: httpx.AsyncClient, sms: FakeSmsSender
) -> None:
    verification_id = await _requested(api)
    code = _code(sms)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        response = await api.post(VERIFY, json={"verification_id": verification_id, "code": wrong})
        assert response.json()["code"] == "VERIFICATION_CODE_INVALID"
    last = await api.post(VERIFY, json={"verification_id": verification_id, "code": code})
    assert (last.status_code, last.json()["code"]) == (410, "VERIFICATION_EXPIRED")


async def test_an_expired_used_or_unknown_verification_is_410(
    api: httpx.AsyncClient, db_session: AsyncSession, sms: FakeSmsSender
) -> None:
    """One answer for all three: nothing tells a guesser which IDs exist."""
    used = await _requested(api)
    used_code = _code(sms)
    assert (
        await api.post(VERIFY, json={"verification_id": used, "code": used_code})
    ).status_code == 200
    expired = await _requested(api, "+251922334455")
    expired_code = _code(sms)
    async with db_session.begin():
        await db_session.execute(
            update(PhoneVerification)
            .where(PhoneVerification.id == uuid.UUID(expired))
            .values(expires_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    for verification_id, code in (
        (used, used_code),
        (expired, expired_code),
        (str(uuid.uuid4()), "123456"),
    ):
        response = await api.post(VERIFY, json={"verification_id": verification_id, "code": code})
        assert (response.status_code, response.json()["code"]) == (410, "VERIFICATION_EXPIRED")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"verification_id": "not-a-uuid", "code": "123456"},
        {"code": "123456"},
        {"verification_id": str(uuid.UUID(int=1))},
        {"verification_id": str(uuid.UUID(int=1)), "code": "12345"},
        {"verification_id": str(uuid.UUID(int=1)), "code": "1234567"},
        {"verification_id": str(uuid.UUID(int=1)), "code": "12a456"},
        {"verification_id": str(uuid.UUID(int=1)), "code": 123456},
        {"verification_id": str(uuid.UUID(int=1)), "code": "１２３４５６"},  # noqa: RUF001
        {"verification_id": str(uuid.UUID(int=1)), "code": "123456", "phone": PHONE},
    ],
    ids=str,
)
async def test_a_bad_verify_body_is_422(api: httpx.AsyncClient, body: object) -> None:
    response = await api.post(VERIFY, json=body)
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


async def test_checking_codes_is_limited_per_caller(
    api: httpx.AsyncClient,
) -> None:
    body = {"verification_id": str(uuid.uuid4()), "code": "123456"}
    for _ in range(30):
        assert (await api.post(VERIFY, json=body)).status_code == 410
    response = await api.post(VERIFY, json=body)
    assert (response.status_code, response.json()["code"]) == (429, "RATE_LIMITED")


async def test_without_redis_no_code_is_checked(api: httpx.AsyncClient, api_app: GizeAPI) -> None:
    async with broken_redis(api_app):
        response = await api.post(
            VERIFY, json={"verification_id": str(uuid.uuid4()), "code": "123456"}
        )
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")


# From review: failures after the commit, the switched-off key, the database, the logs


async def test_a_send_that_breaks_releases_the_key_too(
    api: httpx.AsyncClient, api_app: GizeAPI, sms: FakeSmsSender
) -> None:
    """A bug (or a database blip) mid-send must not leave a stored "sent" behind: the
    retry with the same key sends a code."""

    class Broken:
        async def send(self, to: str, text: str) -> str:
            raise RuntimeError("a bug")

    api_app.state.sms_sender = Broken()
    key = _key()
    response = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    assert response.status_code == 500
    api_app.state.sms_sender = sms
    again = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
    assert again.status_code == 201
    assert "Idempotent-Replayed" not in again.headers
    assert len(sms.sent) == 1


async def test_switched_off_counts_nothing_against_the_limits(
    api: httpx.AsyncClient, api_app: GizeAPI, sms: FakeSmsSender
) -> None:
    switched_on = api_app.state.settings
    api_app.state.settings = switched_on.model_copy(update={"verified_phone_token_key": None})
    for _ in range(12):
        response = await api.post(REQUEST, json={"phone": PHONE}, headers=_key())
        assert response.status_code == 503
    api_app.state.settings = switched_on
    assert (await api.post(REQUEST, json={"phone": PHONE}, headers=_key())).status_code == 201


async def test_the_database_down_while_checking_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.post(VERIFY, json={"verification_id": str(uuid.uuid4()), "code": "123456"})
    assert response.status_code == 503


async def test_no_phone_or_code_reaches_the_logs(
    api: httpx.AsyncClient, sms: FakeSmsSender
) -> None:
    key = _key()
    with structlog.testing.capture_logs() as logs:
        first = await api.post(REQUEST, json={"phone": PHONE}, headers=key)
        await api.post(REQUEST, json={"phone": PHONE}, headers=key)  # a replay logs its scope
        code = _code(sms)
        await api.post(
            VERIFY, json={"verification_id": first.json()["verification_id"], "code": code}
        )
    assert any(log["event"] == "idempotent_replay" for log in logs)
    text = repr(logs)
    for secret in (PHONE, PHONE[4:], code, hashlib.sha256(PHONE.encode()).hexdigest()[:32]):
        assert secret not in text
