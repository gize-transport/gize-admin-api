"""GZ-008: idempotency keys. A retried request never repeats its side effect.

The key is claimed in the same transaction as the business write, so a failed request
leaves no record and a stored record always has its side effect.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.db import get_session
from gize_api.core.idempotency import (
    MAX_STORED_BODY_BYTES,
    IdempotencyKey,
    IdempotentRequest,
    StoredResponse,
    claim,
    idempotent_request,
    purge_expired,
    record,
    replay_response,
)
from gize_api.main import create_app
from tests.db.widgets import Widget
from tests.support import DatabaseUrls, SettingsFactory

pytestmark = pytest.mark.usefixtures("widgets_table")


class WidgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=50)


def _app(make_settings: SettingsFactory, database: DatabaseUrls) -> FastAPI:
    app = create_app(make_settings(database_url=database.app))

    @app.post("/_test/widgets", status_code=201)
    async def create_widget(
        body: WidgetIn,
        request: Request,
        idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        async with session.begin():
            if request.headers.get("x-test-lock-timeout"):  # test-only knob
                await session.execute(text("SET LOCAL lock_timeout = '300ms'"))
            claimed = await claim(session, scope="test", request=idempotent)
            if claimed.stored is not None:
                return replay_response(claimed.stored)
            widget = Widget(label=body.label)
            session.add(widget)
            await session.flush()
            if request.headers.get("x-test-fail"):
                raise RuntimeError("the business write failed")
            if delay := request.headers.get("x-test-delay"):
                await session.execute(select(func.pg_sleep(float(delay))))
            payload = {"id": str(widget.id), "label": widget.label}
            await record(session, claimed, status=201, body=payload)
        return Response(json.dumps(payload), status_code=201, media_type="application/json")

    return app


@pytest.fixture
async def client(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(make_settings, database)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        yield http


async def _widget_count(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session, session.begin():
        return int(await session.scalar(select(func.count()).select_from(Widget)) or 0)


def _headers(key: uuid.UUID, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": str(key), **extra}


async def test_a_replayed_request_returns_the_stored_response_once_executed(
    client: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    key = uuid.uuid4()
    first = await client.post("/_test/widgets", json={"label": "a"}, headers=_headers(key))
    again = await client.post("/_test/widgets", json={"label": "a"}, headers=_headers(key))
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await _widget_count(committed_sessionmaker) == 1


async def test_the_same_body_in_another_key_order_is_the_same_request(
    client: httpx.AsyncClient,
) -> None:
    key = uuid.uuid4()
    json_headers = _headers(key, **{"Content-Type": "application/json"})
    await client.post("/_test/widgets", content=b'{"label": "a"}', headers=json_headers)
    again = await client.post("/_test/widgets", content=b'{ "label" :"a" }', headers=json_headers)
    assert again.status_code == 201
    assert again.headers["Idempotent-Replayed"] == "true"


async def test_the_same_key_with_another_body_is_422(
    client: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    key = uuid.uuid4()
    await client.post("/_test/widgets", json={"label": "a"}, headers=_headers(key))
    response = await client.post("/_test/widgets", json={"label": "b"}, headers=_headers(key))
    assert response.status_code == 422
    assert response.json()["code"] == "IDEMPOTENCY_KEY_MISMATCH"
    assert await _widget_count(committed_sessionmaker) == 1


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", "1234", "x" * 300])
async def test_a_missing_or_malformed_key_is_428(
    client: httpx.AsyncClient, value: str | None
) -> None:
    headers = {} if value is None else {"Idempotency-Key": value}
    response = await client.post("/_test/widgets", json={"label": "a"}, headers=headers)
    assert response.status_code == 428
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_a_failed_request_frees_the_key(
    client: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    key = uuid.uuid4()
    failed = await client.post(
        "/_test/widgets", json={"label": "a"}, headers=_headers(key, **{"x-test-fail": "1"})
    )
    assert failed.status_code == 500
    retried = await client.post("/_test/widgets", json={"label": "a"}, headers=_headers(key))
    assert retried.status_code == 201
    assert "Idempotent-Replayed" not in retried.headers
    assert await _widget_count(committed_sessionmaker) == 1


async def test_parallel_requests_with_one_key_execute_once(
    client: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A double-click: the second request waits for the first, then replays its answer."""
    key = uuid.uuid4()
    slow = client.post(
        "/_test/widgets", json={"label": "a"}, headers=_headers(key, **{"x-test-delay": "0.5"})
    )
    responses = await asyncio.gather(
        slow,
        _after(0.1, client.post("/_test/widgets", json={"label": "a"}, headers=_headers(key))),
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert responses[1].headers["Idempotent-Replayed"] == "true"
    assert await _widget_count(committed_sessionmaker) == 1


async def test_a_request_still_running_is_409_with_retry_after(
    client: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    key = uuid.uuid4()
    slow = client.post(
        "/_test/widgets", json={"label": "a"}, headers=_headers(key, **{"x-test-delay": "1.5"})
    )
    waiting = client.post(
        "/_test/widgets",
        json={"label": "a"},
        headers=_headers(key, **{"x-test-lock-timeout": "1"}),
    )
    first, second = await asyncio.gather(slow, _after(0.2, waiting))
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert second.headers["Retry-After"]
    assert await _widget_count(committed_sessionmaker) == 1


async def _after(seconds: float, request: Awaitable[httpx.Response]) -> httpx.Response:
    await asyncio.sleep(seconds)
    return await request


async def test_keys_are_scoped_per_caller(db_session: AsyncSession) -> None:
    key = IdempotencyKey(uuid.uuid4())
    request = IdempotentRequest(key=key, fingerprint="f" * 64)
    async with db_session.begin():
        first = await claim(db_session, scope="staff:a", request=request)
        await record(db_session, first, status=201, body={"n": 1})
        other = await claim(db_session, scope="staff:b", request=request)
        assert other.stored is None  # another caller's key is not ours to replay
        await record(db_session, other, status=201, body={"n": 2})


async def test_an_unrecorded_claim_cannot_be_committed(db_session: AsyncSession) -> None:
    """Otherwise a replay would find no response: a bug, caught before the commit."""
    request = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint="f" * 64)
    with pytest.raises(RuntimeError, match="not recorded"):
        async with db_session.begin():
            await claim(db_session, scope="test", request=request)


@pytest.mark.parametrize("status", [200, 201, 204])
async def test_only_successful_responses_are_recorded(
    db_session: AsyncSession, status: int
) -> None:
    request = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint="f" * 64)
    async with db_session.begin():
        claimed = await claim(db_session, scope="test", request=request)
        await record(db_session, claimed, status=status, body={})
    async with db_session.begin():
        claimed = await claim(db_session, scope="test", request=request)
        with pytest.raises(ValueError, match="2xx"):
            await record(db_session, claimed, status=400, body={})


async def test_an_expired_key_can_be_used_again(db_session: AsyncSession) -> None:
    request = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint="a" * 64)
    async with db_session.begin():
        claimed = await claim(db_session, scope="test", request=request)
        await record(db_session, claimed, status=201, body={"old": True})
        await db_session.execute(
            text("UPDATE gize.idempotency_keys SET expires_at = now() - interval '1 second'")
        )
    other = IdempotentRequest(key=request.key, fingerprint="b" * 64)  # a different body
    async with db_session.begin():
        claimed = await claim(db_session, scope="test", request=other)
        assert claimed.stored is None
        await record(db_session, claimed, status=201, body={"new": True})


async def test_purge_deletes_only_expired_keys_in_batches(
    committed_sessionmaker: async_sessionmaker[AsyncSession], database: DatabaseUrls
) -> None:
    migrator = await asyncpg.connect(database.migrator, timeout=3)
    now = datetime.now(UTC)
    try:
        await migrator.executemany(
            "INSERT INTO gize.idempotency_keys"
            " (scope, key, fingerprint, response_status, response_body, expires_at)"
            " VALUES ('test', $1, $2, 201, '{}', $3)",
            [(uuid.uuid4(), "f" * 64, now - timedelta(hours=1)) for _ in range(25)]
            + [(uuid.uuid4(), "f" * 64, now + timedelta(hours=1)) for _ in range(3)],
        )
        deleted = await purge_expired(committed_sessionmaker, batch_size=10)
        remaining = await migrator.fetchval("SELECT count(*) FROM gize.idempotency_keys")
    finally:
        await migrator.close()
    assert deleted == 25
    assert remaining == 3


async def test_other_database_errors_are_not_disguised(db_session: AsyncSession) -> None:
    """Only a lock wait means "in progress"; anything else stays what it is."""
    request = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint="f" * 64)
    with pytest.raises(DBAPIError):
        async with db_session.begin():
            await claim(db_session, scope="s" * 101, request=request)  # longer than the column


async def test_responses_too_large_to_store_are_refused(db_session: AsyncSession) -> None:
    request = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint="f" * 64)
    async with db_session.begin():
        claimed = await claim(db_session, scope="test", request=request)
        with pytest.raises(ValueError, match="too large"):
            await record(db_session, claimed, status=201, body={"x": "y" * MAX_STORED_BODY_BYTES})
        await record(db_session, claimed, status=201, body={})


def test_a_replayed_204_has_no_body() -> None:
    response = replay_response(StoredResponse(status=204, body=None))
    assert response.status_code == 204
    assert response.body == b""
    assert response.headers["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize(
    "value",
    [
        "{12345678-1234-5678-1234-567812345678}",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
        "12345678123456781234567812345678",
    ],
)
async def test_only_the_canonical_key_form_is_accepted(
    client: httpx.AsyncClient, value: str
) -> None:
    response = await client.post(
        "/_test/widgets", json={"label": "a"}, headers={"Idempotency-Key": value}
    )
    assert response.status_code == 428


def test_non_json_bodies_are_compared_as_bytes() -> None:
    from gize_api.core.idempotency import _canonical

    assert _canonical(b"not json") == b"not json"
    assert _canonical(b'{"b": 1, "a": 2}') == b'{"a":2,"b":1}'


async def test_purge_stops_after_its_batch_limit(
    committed_sessionmaker: async_sessionmaker[AsyncSession], database: DatabaseUrls
) -> None:
    """One run never loops forever; the next run continues."""
    migrator = await asyncpg.connect(database.migrator, timeout=3)
    expired = datetime.now(UTC) - timedelta(hours=1)
    try:
        await migrator.executemany(
            "INSERT INTO gize.idempotency_keys"
            " (scope, key, fingerprint, response_status, response_body, expires_at)"
            " VALUES ('test', $1, $2, 201, '{}', $3)",
            [(uuid.uuid4(), "f" * 64, expired) for _ in range(5)],
        )
        assert await purge_expired(committed_sessionmaker, batch_size=2, max_batches=2) == 4
        assert await purge_expired(committed_sessionmaker, batch_size=2, max_batches=2) == 1
    finally:
        await migrator.close()
