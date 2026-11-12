"""GZ-008: cursor pagination: opaque cursors, bounded limits, strict query parameters."""

import base64
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.pagination import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    Cursor,
    PageParams,
    decode_cursor,
    encode_cursor,
    page_params,
)
from gize_api.main import create_app
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
        "?cursor=" + "a" * 201,
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
