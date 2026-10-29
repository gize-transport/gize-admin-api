"""Idempotency keys: a retried side-effecting POST never repeats its side effect.

The client sends `Idempotency-Key` (a UUID, api-contract.md), reused on every retry of one
user action. The service claims the key **in the same transaction as its business write**:

    async with session.begin():
        claimed = await claim(session, scope=staff_scope, request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        ... the business write ...
        await record(session, claimed, status=201, body=payload)

- A failed request rolls back, freeing the key: no side effect without a record, and no
  record without its side effect. Only 2xx responses are recorded.
- A concurrent duplicate waits on the key's row until the first commits, then replays its
  response; if the first is still running after the lock timeout, it gets 409.
- The same key with a different body or endpoint is 422; an expired key (24 h) is free.
- A claim that is never recorded makes the commit fail (a bug caught before it is stored).
  So a failure must raise (an AppError for a client error): it rolls the claim back.
- `claim` comes first in the block: a replay returns from the block, which commits, and
  earlier writes would be committed with it. Writing before the claim raises.
- Keys are scoped per caller (`scope`), so two callers' keys never meet. The fingerprint
  covers the method, the endpoint, the actual path (its IDs), the query and the body.
- A replay returns the recorded status, body and headers (Location, Content-Location, ETag).

`purge_expired` is the worker's cleanup job.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, NewType, cast
from urllib.parse import parse_qsl, urlencode

import structlog
from fastapi import Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import (
    DateTime,
    SmallInteger,
    String,
    Table,
    delete,
    event,
    func,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, Session, SessionTransaction, mapped_column

from gize_api.core.db import GizeSession, database_sqlstate
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.models import Base

IDEMPOTENCY_TTL = timedelta(hours=24)
REPLAYED_HEADERS = frozenset({"location", "content-location", "etag"})
MAX_STORED_BODY_BYTES = 64 * 1024
IN_PROGRESS_RETRY_AFTER_SECONDS = "1"
PURGE_BATCH_SIZE = 1000
PURGE_MAX_BATCHES = 100
_LOCK_NOT_AVAILABLE = "55P03"
_PENDING = "gize_idempotency_pending"

IdempotencyKey = NewType("IdempotencyKey", uuid.UUID)

logger = structlog.get_logger("gize_api.idempotency")


class IdempotencyRecord(Base):
    """A claimed key and, once recorded, the response it produced."""

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    key: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # NULL while claimed; set by `record` before the claiming transaction commits.
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[Any] = mapped_column(JSONB, nullable=True)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True)
class IdempotentRequest:
    """The key and a fingerprint of what was asked (method, endpoint, canonical body)."""

    key: IdempotencyKey
    fingerprint: str


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Claimed:
    scope: str
    key: IdempotencyKey
    stored: StoredResponse | None  # set: replay it instead of doing the work again


async def idempotent_request(
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IdempotentRequest:
    """FastAPI dependency for side-effecting POSTs: the key (required) and the fingerprint."""
    key = _parse_key(idempotency_key)
    body = await request.body()
    route = getattr(request.scope.get("route"), "path", "")
    query = urlencode(sorted(parse_qsl(request.url.query, keep_blank_values=True)))
    digest = hashlib.sha256()
    for part in (
        request.method.encode(),
        route.encode(),
        request.url.path.encode(),  # the IDs in the path: another resource, another request
        query.encode(),
        _canonical(body),
    ):
        digest.update(len(part).to_bytes(8, "big") + part)
    return IdempotentRequest(key=key, fingerprint=digest.hexdigest())


async def claim(session: AsyncSession, *, scope: str, request: IdempotentRequest) -> Claimed:
    """Claim the key inside the caller's transaction, or return the response to replay."""
    sync_session = session.sync_session
    if sync_session.new or sync_session.dirty or sync_session.deleted:
        raise RuntimeError("claim must come first in the transaction, before any write")
    table = cast(Table, IdempotencyRecord.__table__)
    statement = (
        pg_insert(IdempotencyRecord)
        .values(
            scope=scope,
            key=request.key,
            fingerprint=request.fingerprint,
            expires_at=func.now() + IDEMPOTENCY_TTL,
        )
        .on_conflict_do_update(  # an expired key is free again
            index_elements=[table.c.scope, table.c.key],
            set_={
                "fingerprint": request.fingerprint,
                "response_status": None,
                "response_body": None,
                "response_headers": None,
                "created_at": func.now(),
                "expires_at": func.now() + IDEMPOTENCY_TTL,
            },
            where=table.c.expires_at < func.now(),
        )
        .returning(table.c.key)
    )
    try:
        claimed_key = await session.scalar(statement)
    except DBAPIError as exc:
        if database_sqlstate(exc) != _LOCK_NOT_AVAILABLE:
            raise
        logger.info("idempotency_in_progress", scope=scope)
        raise AppError(
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            headers={"Retry-After": IN_PROGRESS_RETRY_AFTER_SECONDS},
        ) from None
    if claimed_key is not None:
        session.sync_session.info.setdefault(_PENDING, set()).add((scope, request.key))
        return Claimed(scope=scope, key=request.key, stored=None)
    existing = (
        await session.execute(
            select(
                table.c.fingerprint,
                table.c.response_status,
                table.c.response_body,
                table.c.response_headers,
            ).where(table.c.scope == scope, table.c.key == request.key)
        )
    ).one()
    if existing.fingerprint != request.fingerprint:
        logger.warning("idempotency_key_mismatch", scope=scope)
        raise AppError(ErrorCode.IDEMPOTENCY_KEY_MISMATCH)
    if existing.response_status is None:  # pragma: no cover - prevented by the commit check
        raise RuntimeError("idempotency key committed without a response")
    logger.info("idempotent_replay", scope=scope)
    return Claimed(
        scope=scope,
        key=request.key,
        stored=StoredResponse(
            status=existing.response_status,
            body=existing.response_body,
            headers=existing.response_headers or {},
        ),
    )


async def record(
    session: AsyncSession,
    claimed: Claimed,
    *,
    status: int,
    body: Any,  # noqa: ANN401 - JSON
    headers: Mapping[str, str] | None = None,
) -> None:
    """Store the successful response for replays, before the transaction commits."""
    if not 200 <= status < 300:
        raise ValueError("only 2xx responses are recorded; failures roll back instead")
    headers = dict(headers or {})
    if {name.lower() for name in headers} - REPLAYED_HEADERS:
        raise ValueError(f"only these headers are replayed: {sorted(REPLAYED_HEADERS)}")
    if len(json.dumps(body, separators=(",", ":"))) > MAX_STORED_BODY_BYTES:
        raise ValueError("response too large to store for replays")
    table = cast(Table, IdempotencyRecord.__table__)
    result = await session.execute(
        update(table)
        .where(table.c.scope == claimed.scope, table.c.key == claimed.key)
        .values(response_status=status, response_body=body, response_headers=headers or None)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        raise RuntimeError("no claim to record: call claim() first, in the same transaction")
    session.sync_session.info.get(_PENDING, set()).discard((claimed.scope, claimed.key))


def replay_response(stored: StoredResponse) -> Response:
    headers = {**stored.headers, "Idempotent-Replayed": "true"}
    if stored.status == 204:
        return Response(status_code=204, headers=headers)
    return JSONResponse(stored.body, status_code=stored.status, headers=headers)


async def purge_expired(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = PURGE_BATCH_SIZE,
    max_batches: int = PURGE_MAX_BATCHES,
) -> int:
    """Delete expired keys in small transactions (never one long lock); returns the count."""
    table = cast(Table, IdempotencyRecord.__table__)
    expired = (
        select(table.c.scope, table.c.key)
        .where(table.c.expires_at < func.now())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    total = 0
    for _ in range(max_batches):
        async with sessionmaker() as session, session.begin():
            result = await session.execute(
                delete(table).where(tuple_(table.c.scope, table.c.key).in_(expired))
            )
        deleted = cast(CursorResult[Any], result).rowcount
        total += deleted
        if deleted < batch_size:
            break
    return total


@event.listens_for(GizeSession, "before_commit")
def _refuse_unrecorded_claims(session: Session) -> None:
    # Only the real commit counts: a savepoint between claim and record is fine.
    if session.in_nested_transaction():
        return
    if session.info.get(_PENDING):
        session.info[_PENDING].clear()
        raise RuntimeError("an idempotency claim was not recorded before commit")


@event.listens_for(GizeSession, "after_transaction_end")
def _forget_claims(session: Session, transaction: SessionTransaction) -> None:
    # Only when the whole transaction ends: a rolled-back savepoint must not hide a claim.
    if transaction.parent is None:
        session.info.pop(_PENDING, None)


def _parse_key(value: str | None) -> IdempotencyKey:
    try:
        key = uuid.UUID(value or "")
    except ValueError:
        raise AppError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED) from None
    if str(key) != (value or "").lower():  # the canonical 8-4-4-4-12 form only
        raise AppError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    return IdempotencyKey(key)


def _canonical(body: bytes) -> bytes:
    """JSON bodies compare by content (key order and spacing do not matter)."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
