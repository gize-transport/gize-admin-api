"""GZ-008: cursor pagination: opaque cursors, bounded limits, strict query parameters.

GZ-081: a list sorted another way than its default pages by that key, and its cursor
names the sort: one made for another sort is INVALID_CURSOR. GZ-082: a key may have
several parts (a corridor's two area names) and a longer text (a staff name)."""

import base64
import json
import uuid
from datetime import UTC, datetime, time

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.pagination import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    Cursor,
    KeyCursor,
    KeyKind,
    Order,
    PageParams,
    SortKey,
    decode_cursor,
    decode_key_cursor,
    encode_cursor,
    encode_key_cursor,
    page_params,
    paginate,
)
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH
from gize_api.main import create_app
from gize_api.staff.models import Staff
from tests.support import SettingsFactory


def test_cursor_round_trip_is_opaque_and_short() -> None:
    cursor = Cursor(created_at=datetime(2026, 10, 28, 6, 30, tzinfo=UTC), id=uuid.uuid4())
    token = encode_cursor(cursor)
    assert decode_cursor(token) == cursor
    assert len(token) <= MAX_CURSOR_LENGTH
    for character in "=+/":  # URL-safe and unpadded
        assert character not in token


def _token(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "token",
    [
        "not base64 !",
        _token("just a string"),
        _token(["2026-10-28T06:30:00+00:00"]),
        _token(["2026-10-28T06:30:00+00:00", "not-a-uuid"]),
        _token(["2026-10-28T06:30:00", str(uuid.uuid4())]),  # no time zone
        _token(["yesterday", str(uuid.uuid4())]),
        _token(["2026-10-28T06:30:00+00:00", str(uuid.uuid4()), "extra"]),
        _token(["2026-10-28T06:30:00+00:00", 5]),  # an id that isn't text
        _token(["2026-10-28T06:30:00+00:00", [str(uuid.uuid4())]]),
        _token([20261028, str(uuid.uuid4())]),  # a time that isn't text
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        _token(["0001-01-01T00:00:00+14:00", str(uuid.uuid4())]),  # outside timestamptz
        _token(["9999-12-31T23:59:59-14:00", str(uuid.uuid4())]),
        "",
    ],
)
def test_tampered_cursors_are_invalid(token: str) -> None:
    with pytest.raises(AppError) as exc_info:
        decode_cursor(token)
    assert exc_info.value.code is ErrorCode.INVALID_CURSOR


def _app(make_settings: SettingsFactory) -> FastAPI:
    app = create_app(make_settings())

    @app.get("/_test/list")
    async def listing(params: PageParams = Depends(page_params)) -> dict[str, object]:  # noqa: B008
        return {"limit": params.limit, "cursor": params.cursor is not None}

    return app


async def _get(app: FastAPI, query: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/_test/list{query}")


async def test_limit_defaults_and_accepts_the_maximum(make_settings: SettingsFactory) -> None:
    app = _app(make_settings)
    assert (await _get(app, "")).json() == {"limit": DEFAULT_LIMIT, "cursor": False}
    assert (await _get(app, f"?limit={MAX_LIMIT}")).json()["limit"] == MAX_LIMIT


@pytest.mark.parametrize(
    "query",
    [
        f"?limit={MAX_LIMIT + 1}",
        "?limit=0",
        "?limit=-1",
        "?limit=ten",
        "?sort=name",
        "?cursor=" + "a" * (MAX_CURSOR_LENGTH + 1),
    ],
)
async def test_invalid_page_parameters_are_422(make_settings: SettingsFactory, query: str) -> None:
    response = await _get(_app(make_settings), query)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_a_bad_cursor_is_422_invalid_cursor(make_settings: SettingsFactory) -> None:
    response = await _get(_app(make_settings), "?cursor=bm90LWEtY3Vyc29y")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_CURSOR"
    assert "bm90" not in response.text  # the cursor is not echoed


NAME = Order("name", KeyKind.TEXT, ignore_case=True)
MORNING = Order("morning_time", KeyKind.TIME)
NEWEST = Order("newest", KeyKind.TIMESTAMP, descending=True)
CORRIDOR = Order("name", KeyKind.TEXT, ignore_case=True, parts=2)
STAFF = Order("name", KeyKind.TEXT, ignore_case=True, max_length=200)
ID = uuid.uuid4()


@pytest.mark.parametrize(
    ("order", "value"),
    [
        (NAME, "Abebe Kebede"),
        (NAME, "አበበ ከበደ"),
        (MORNING, time(6, 30)),
        (NEWEST, datetime(2026, 11, 20, 6, 30, 15, 123456, tzinfo=UTC)),
    ],
    ids=str,
)
def test_key_cursors_round_trip(order: Order, value: str | time | datetime) -> None:
    token = encode_key_cursor(order, (value,), ID)
    assert decode_key_cursor(token, order) == KeyCursor(values=(value,), id=ID)
    for character in "=+/":
        assert character not in token


def test_a_key_of_two_parts_round_trips() -> None:
    token = encode_key_cursor(CORRIDOR, ("Bole", "Mexico"), ID)
    assert decode_key_cursor(token, CORRIDOR) == KeyCursor(values=("Bole", "Mexico"), id=ID)


@pytest.mark.parametrize(
    ("order", "values"),
    [
        (NAME, ("\U0001f68c" * SHORT_TEXT_MAX_LENGTH,)),  # a name: 100 characters
        (STAFF, ("\U0001f68c" * 200,)),  # a staff name: 200
        (CORRIDOR, ("\U0001f68c" * SHORT_TEXT_MAX_LENGTH,) * 2),  # two area names
    ],
    ids=["name", "staff", "corridor"],
)
def test_the_longest_keys_fit_in_a_cursor(order: Order, values: tuple[str, ...]) -> None:
    """Any script: four bytes a character at most in UTF-8."""
    token = encode_key_cursor(order, values, ID)
    assert len(token) <= MAX_CURSOR_LENGTH
    assert PageParams(cursor=token).cursor == token
    assert decode_key_cursor(token, order).values == values


@pytest.mark.parametrize(
    ("order", "token"),
    [
        (NAME, encode_cursor(Cursor(created_at=datetime(2026, 10, 28, tzinfo=UTC), id=ID))),
        (MORNING, encode_key_cursor(NAME, ("Abebe",), ID)),  # made for another sort
        (NEWEST, encode_key_cursor(Order("oldest", KeyKind.TIMESTAMP), (datetime.now(UTC),), ID)),
        (STAFF, _token(["name", "x" * 201, str(ID)])),
        (CORRIDOR, encode_key_cursor(NAME, ("Bole",), ID)),  # one part for two
        (CORRIDOR, _token(["name", ["Bole"], str(ID)])),
        (CORRIDOR, _token(["name", ["Bole", "Mexico", "CMC"], str(ID)])),
        (CORRIDOR, _token(["name", ["Bole", 5], str(ID)])),
        (CORRIDOR, _token(["name", ["Bole", "\u0000"], str(ID)])),
        (NAME, _token(["name", ["Bole"], str(ID)])),  # a list for one part
        (NAME, _token(["name", "Abebe"])),
        (NAME, _token(["name", "Abebe", str(ID), "extra"])),
        (NAME, _token(["name", 5, str(ID)])),
        (NAME, _token(["name", "Abe\u0000be", str(ID)])),  # the database refuses a NUL
        (NAME, _token(["name", "\ud800", str(ID)])),  # a lone surrogate isn't UTF-8
        (NAME, _token(["name", "Abebe\nKebede", str(ID)])),
        (NAME, _token(["name", "x" * (SHORT_TEXT_MAX_LENGTH + 1), str(ID)])),
        (NAME, _token(["name", "Abebe", "not-a-uuid"])),
        (NAME, _token(["name", "Abebe", 5])),
        (NAME, _token({"name": "Abebe"})),
        (MORNING, _token(["morning_time", "six thirty", str(ID)])),
        (MORNING, _token(["morning_time", "06:30:00+03:00", str(ID)])),  # a time zone
        (NEWEST, _token(["newest", "2026-10-28T06:30:00", str(ID)])),  # no time zone
        (NEWEST, _token(["newest", "0001-01-01T00:00:00+14:00", str(ID)])),
        (NAME, "not base64 !"),
        (NAME, base64.urlsafe_b64encode(b"\xff\xfe").decode()),
        (NAME, ""),
    ],
)
def test_tampered_or_foreign_key_cursors_are_invalid(order: Order, token: str) -> None:
    with pytest.raises(AppError) as exc_info:
        decode_key_cursor(token, order)
    assert exc_info.value.code is ErrorCode.INVALID_CURSOR


def test_a_sorted_lists_cursor_is_invalid_in_the_default_order() -> None:
    with pytest.raises(AppError) as exc_info:
        decode_cursor(encode_key_cursor(NAME, ("Abebe",), ID))
    assert exc_info.value.code is ErrorCode.INVALID_CURSOR


async def test_an_order_with_no_column_is_a_wiring_bug(db_session: AsyncSession) -> None:
    """A list offering a sort its service gives no column for fails loudly, never pages
    some other way."""

    class Sorted(PageParams):
        @property
        def order(self) -> Order | None:
            return NAME

    with pytest.raises(LookupError):
        await paginate(db_session, select(Staff), Sorted(), created_at=Staff.created_at,
                       id=Staff.id, sort_keys={"code": Staff.full_name})  # fmt: skip


async def test_a_sort_key_with_the_wrong_number_of_parts_is_a_wiring_bug(
    db_session: AsyncSession,
) -> None:
    class Sorted(PageParams):
        @property
        def order(self) -> Order | None:
            return CORRIDOR

    key = SortKey(columns=(Staff.full_name,), read=lambda row: (row.full_name,))
    with pytest.raises(LookupError):
        await paginate(db_session, select(Staff), Sorted(), created_at=Staff.created_at,
                       id=Staff.id, sort_keys={"name": key})  # fmt: skip
