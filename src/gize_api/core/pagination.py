"""Cursor (keyset) pagination: `?limit=&cursor=` → `{items, next_cursor}` (api-contract.md).

The cursor is opaque to clients: the `created_at` and `id` of the last row served, as
unpadded base64url JSON. It carries no secret and grants nothing: a client that forges one
only chooses where its own, already authorized, list starts. Rows are ordered by
`(created_at, id)`, so equal timestamps never repeat or skip a row; list tables need an
index on those two columns.

Endpoints without filters take `PageParams` through `page_params`. Endpoints with filters
subclass PageParams (it forbids unknown parameters) and call `params.after` early, so a bad
cursor is rejected before any work.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from gize_api.core.errors import AppError, ErrorCode

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_CURSOR_LENGTH = 200


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: uuid.UUID


class PageParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH)

    @property
    def after(self) -> Cursor | None:
        """The decoded cursor; raises AppError(INVALID_CURSOR) if it was tampered with."""
        return None if self.cursor is None else decode_cursor(self.cursor)


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
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        raise AppError(ErrorCode.INVALID_CURSOR) from None
    if not token or created_at.tzinfo is None:
        raise AppError(ErrorCode.INVALID_CURSOR)
    return Cursor(created_at=created_at, id=cursor_id)


async def paginate[M](
    session: AsyncSession,
    statement: Select[M],
    params: PageParams,
    *,
    created_at: InstrumentedAttribute[datetime],
    id: InstrumentedAttribute[uuid.UUID],
    newest_first: bool = False,
) -> Slice[M]:
    """One page of `statement` (filters already applied), fetching one row extra to learn
    whether another page exists. Call inside the session's transaction block."""
    after = params.after
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
