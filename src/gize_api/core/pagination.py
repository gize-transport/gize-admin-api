"""Cursor (keyset) pagination: `?limit=&cursor=` → `{items, next_cursor}` (api-contract.md).

The cursor is opaque to clients: the `created_at` and `id` of the last row served, as
unpadded base64url JSON. It carries no secret and grants nothing: a client that forges one
only chooses where its own, already authorized, list starts. Rows are ordered by
`(created_at, id)`, so equal timestamps never repeat or skip a row; list tables need an
index on those two columns.

Endpoints without filters take `PageParams` through `page_params`. Endpoints with filters
subclass PageParams (it forbids unknown parameters) and call `params.after` early, so a bad
cursor is rejected before any work.

Sorting (GZ-081): a list may offer other orders through a `sort` parameter, its subclass
returning the chosen `Order` from `order` (None: the list's default, `created_at` then
`id`, whose cursors keep the form above). Such an order pages by its key, then `id`, and
its cursor names it: `[tag, key, id]`. A cursor made for one order is INVALID_CURSOR in
any other, so a client can't page one sort with another's position. Each order's key
needs an index with `id`, like the default's.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, Select, func, literal, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, no_control_characters

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
# A sorted list's cursor holds its key: a 100-character name in any script fits (GZ-081).
MAX_CURSOR_LENGTH = 700


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: uuid.UUID


class KeyKind(StrEnum):
    """What a sort key holds, as a cursor carries it."""

    TEXT = "text"
    TIME = "time"  # a local time of day, no time zone
    TIMESTAMP = "timestamp"  # an instant, with its time zone


@dataclass(frozen=True)
class Order:
    """A sort a list offers besides its default: `tag` names it in cursors. `ignore_case`
    sorts a text key by `lower`, the same on both sides of the page boundary (its index
    must be on `lower` too)."""

    tag: str
    kind: KeyKind
    descending: bool = False
    ignore_case: bool = False


@dataclass(frozen=True)
class KeyCursor:
    """Where a sorted page starts after: the last row's key, as stored, and its id."""

    value: str | time | datetime
    id: uuid.UUID


class PageParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH)

    @property
    def order(self) -> Order | None:
        """The sort chosen; None is the list's default (subclasses with `sort` override)."""
        return None

    @property
    def after(self) -> Cursor | KeyCursor | None:
        """The decoded cursor; raises AppError(INVALID_CURSOR) if it was tampered with or
        made for another order."""
        if self.cursor is None:
            return None
        order = self.order
        return (
            decode_cursor(self.cursor) if order is None else decode_key_cursor(self.cursor, order)
        )


def page_params(params: Annotated[PageParams, Query()]) -> PageParams:
    """FastAPI dependency: validated page parameters, cursor included."""
    _ = params.after
    return params


class Page[T](BaseModel):
    """A response page: `items` and the cursor for the next page (None on the last)."""

    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True)
class Slice[M]:
    """Rows of one page, as `paginate` returns them (convert items to schemas for Page)."""

    items: list[M]
    next_cursor: str | None


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps([cursor.created_at.isoformat(), str(cursor.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode_cursor(token: str) -> Cursor:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        created_at_text, id_text = json.loads(raw.decode())
        created_at = datetime.fromisoformat(created_at_text)
        cursor_id = uuid.UUID(id_text)
        if not token or created_at.tzinfo is None:
            raise ValueError("no time zone")
        created_at = created_at.astimezone(UTC)  # out-of-range instants fail here, not in SQL
    except (
        binascii.Error,
        ValueError,
        TypeError,
        AttributeError,
        UnicodeDecodeError,
        OverflowError,
    ):
        # AttributeError: uuid.UUID given something not text (a number, a list).
        raise AppError(ErrorCode.INVALID_CURSOR) from None
    return Cursor(created_at=created_at, id=cursor_id)


def _b64(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def encode_key_cursor(order: Order, value: str | time | datetime, row_id: uuid.UUID) -> str:
    key = value if isinstance(value, str) else value.isoformat()
    return _b64([order.tag, key, str(row_id)])


def decode_key_cursor(token: str, order: Order) -> KeyCursor:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())
        if not token or not isinstance(payload, list) or len(payload) != 3:
            raise ValueError("not a sorted cursor")
        tag, key, id_text = payload
        if tag != order.tag or not isinstance(key, str) or not isinstance(id_text, str):
            raise ValueError("another order's cursor")
        value: str | time | datetime = key
        if order.kind is KeyKind.TEXT:
            # Stored text never holds control characters (a NUL or a lone surrogate would
            # fail in the database, a 500) nor more than a short text's length.
            no_control_characters(key)
            if len(key) > SHORT_TEXT_MAX_LENGTH:
                raise ValueError("longer than any stored key")
        elif order.kind is KeyKind.TIME:
            value = time.fromisoformat(key)
            if value.tzinfo is not None:
                raise ValueError("a time of day has no time zone")
        else:  # KeyKind.TIMESTAMP
            value = datetime.fromisoformat(key)
            if value.tzinfo is None:
                raise ValueError("no time zone")
            value = value.astimezone(UTC)  # out-of-range instants fail here, not in SQL
        row_id = uuid.UUID(id_text)
    except (binascii.Error, ValueError, UnicodeDecodeError, OverflowError):
        raise AppError(ErrorCode.INVALID_CURSOR) from None
    return KeyCursor(value=value, id=row_id)


async def paginate[M](
    session: AsyncSession,
    statement: Select[M],
    params: PageParams,
    *,
    created_at: InstrumentedAttribute[datetime],
    id: InstrumentedAttribute[uuid.UUID],
    newest_first: bool = False,
    sort_keys: dict[str, InstrumentedAttribute[Any]] | None = None,
) -> Slice[M]:
    """One page of `statement`, fetching one row extra to learn whether another page exists.

    `statement` selects one entity with its filters applied and no ORDER BY (the order is
    set here). The default order is `created_at` (oldest first, or newest), then `id`;
    `sort_keys` gives the column of each other order the params may choose, by tag. Call
    inside the session's transaction block.
    """
    chosen = params.order
    if chosen is not None:
        if sort_keys is None or chosen.tag not in sort_keys:
            raise LookupError(f"no column for the order {chosen.tag!r}")  # a wiring bug
        return await _paginate_by(session, statement, params, chosen, sort_keys[chosen.tag], id)
    after = decode_cursor(params.cursor) if params.cursor is not None else None
    if after is not None:
        position = tuple_(created_at, id)
        boundary = tuple_(after.created_at, after.id)
        statement = statement.where(position < boundary if newest_first else position > boundary)
    order = (created_at.desc(), id.desc()) if newest_first else (created_at.asc(), id.asc())
    rows = list((await session.scalars(statement.order_by(*order).limit(params.limit + 1))).all())
    if len(rows) <= params.limit:
        return Slice(items=rows, next_cursor=None)
    rows = rows[: params.limit]
    last = rows[-1]
    return Slice(
        items=rows,
        next_cursor=encode_cursor(
            Cursor(created_at=getattr(last, created_at.key), id=getattr(last, id.key))
        ),
    )


def _sort_key(order: Order, value: Any) -> ColumnElement[Any]:  # noqa: ANN401
    return func.lower(value) if order.ignore_case else value


async def _paginate_by[M](
    session: AsyncSession,
    statement: Select[M],
    params: PageParams,
    order: Order,
    column: InstrumentedAttribute[Any],
    id: InstrumentedAttribute[uuid.UUID],
) -> Slice[M]:
    key = _sort_key(order, column)
    if params.cursor is not None:
        after = decode_key_cursor(params.cursor, order)
        position = tuple_(key, id)
        boundary = tuple_(_sort_key(order, literal(after.value, column.type)), after.id)
        statement = statement.where(
            position < boundary if order.descending else position > boundary
        )
    ordering = (key.desc(), id.desc()) if order.descending else (key.asc(), id.asc())
    rows = list(
        (await session.scalars(statement.order_by(*ordering).limit(params.limit + 1))).all()
    )
    if len(rows) <= params.limit:
        return Slice(items=rows, next_cursor=None)
    rows = rows[: params.limit]
    last = rows[-1]
    return Slice(
        items=rows,
        next_cursor=encode_key_cursor(order, getattr(last, column.key), getattr(last, id.key)),
    )
