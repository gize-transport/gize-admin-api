"""Scaffold checks for GZ-001: the package is installed, typed and serves an ASGI app."""

from importlib import resources

import httpx
import pytest

import gize_api
from gize_api.main import app


def test_package_importable() -> None:
    assert gize_api.__name__ == "gize_api"


def test_package_ships_py_typed_marker() -> None:
    assert resources.files(gize_api).joinpath("py.typed").is_file()


@pytest.mark.parametrize("path", ["/", "/docs", "/redoc", "/openapi.json"])
async def test_app_exposes_no_routes_or_docs_yet(path: str) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
