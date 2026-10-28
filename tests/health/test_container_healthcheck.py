"""GZ-007: the image's HEALTHCHECK probes liveness, and only in the API container."""

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from gize_api.healthcheck import is_api_process, main

ROOT = Path(__file__).resolve().parents[2]


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status if self.path == "/health/live" else 404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[tuple[str, type[_Handler]]]:
    handler = type("Handler", (_Handler,), {"status": 200})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/health/live", handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cmdline(tmp_path: Path, *args: str) -> Path:
    path = tmp_path / "cmdline"
    path.write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    return path


def test_recognises_the_api_process(tmp_path: Path) -> None:
    api = _cmdline(tmp_path, "/opt/venv/bin/python3", "/opt/venv/bin/uvicorn", "--factory")
    assert is_api_process(api)
    worker = _cmdline(tmp_path, "/opt/venv/bin/python3", "/opt/venv/bin/arq", "x")
    assert not is_api_process(worker)
    assert is_api_process(tmp_path / "missing")  # unknown: probe rather than assume


def test_healthy_when_live_answers(tmp_path: Path, server: tuple[str, type[_Handler]]) -> None:
    url, _ = server
    assert main(_cmdline(tmp_path, "uvicorn"), url=url) == 0


def test_unhealthy_when_live_fails_or_nothing_listens(
    tmp_path: Path, server: tuple[str, type[_Handler]], blackhole_url: str
) -> None:
    url, handler = server
    handler.status = 500
    assert main(_cmdline(tmp_path, "uvicorn"), url=url) == 1
    assert main(_cmdline(tmp_path, "uvicorn"), url="http://127.0.0.1:1/health/live") == 1
    assert main(_cmdline(tmp_path, "uvicorn"), url=blackhole_url, timeout=0.2) == 1


def test_other_containers_are_healthy_without_probing(tmp_path: Path) -> None:
    assert main(_cmdline(tmp_path, "arq"), url="http://127.0.0.1:1/never") == 0


def test_dockerfile_declares_the_healthcheck() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile
    assert '"python", "-m", "gize_api.healthcheck"' in dockerfile
