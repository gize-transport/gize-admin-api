"""GZ-007: the image's HEALTHCHECK probes liveness, and only in the API container."""

import asyncio
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from gize_api.healthcheck import main, runs_without_http

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


def _cmdline(tmp_path: Path, *processes: tuple[str, ...]) -> Path:
    """A fake /proc with one numbered directory per process."""
    proc = tmp_path / "proc"
    for pid, args in enumerate(processes, start=1):
        (proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (proc / "self").mkdir(parents=True, exist_ok=True)  # not a process directory
    return proc


@pytest.mark.parametrize(
    ("processes", "skip"),
    [
        ([("/opt/venv/bin/python3", "/opt/venv/bin/uvicorn", "--factory")], False),
        ([("/sbin/docker-init", "--", "/usr/local/bin/entrypoint.sh", "api")], False),
        (
            [
                ("/sbin/docker-init", "--", "entrypoint.sh", "worker"),
                ("python3", "/opt/venv/bin/arq", "x"),
            ],
            True,
        ),
        ([("python3", "/opt/venv/bin/alembic", "upgrade", "head")], True),
        ([("python", "-m", "gize_api.worker")], True),  # the image's worker command
        ([], False),  # nothing readable: probe rather than assume
    ],
)
def test_probes_unless_a_worker_or_migration_is_found(
    tmp_path: Path, processes: list[tuple[str, ...]], skip: bool
) -> None:
    """Under an init process (docker --init, ECS) PID 1 is not the app: look at every process."""
    assert runs_without_http(_cmdline(tmp_path, *processes)) is skip


def test_missing_proc_means_probe(tmp_path: Path) -> None:
    assert runs_without_http(tmp_path / "no-proc") is False


def test_unreadable_processes_are_skipped(tmp_path: Path) -> None:
    """A process can end, or belong to another user, between listing and reading."""
    proc = _cmdline(tmp_path, ("python3", "/opt/venv/bin/uvicorn"))
    (proc / "7" / "cmdline").mkdir(parents=True)  # reading it fails (not a file)
    assert runs_without_http(proc) is False  # every entry was looked at; none is a worker
    (proc / "8").mkdir()
    (proc / "8" / "cmdline").write_bytes(b"python3\0/opt/venv/bin/arq\0")
    assert runs_without_http(proc) is True  # whatever order the entries are listed in


def test_healthy_when_live_answers(tmp_path: Path, server: tuple[str, type[_Handler]]) -> None:
    url, _ = server
    assert main(_cmdline(tmp_path, ("uvicorn",)), url=url) == 0


def test_unhealthy_when_live_fails_or_nothing_listens(
    tmp_path: Path, server: tuple[str, type[_Handler]], blackhole_url: str
) -> None:
    url, handler = server
    handler.status = 500
    assert main(_cmdline(tmp_path, ("uvicorn",)), url=url) == 1
    assert main(_cmdline(tmp_path, ("uvicorn",)), url="http://127.0.0.1:1/health/live") == 1
    assert main(_cmdline(tmp_path, ("uvicorn",)), url=blackhole_url, timeout=0.2) == 1


def test_a_worker_container_is_never_probed_over_http(
    tmp_path: Path, server: tuple[str, type[_Handler]]
) -> None:
    """It serves no HTTP: only its health key in Redis counts (see the worker test below)."""
    url, _ = server  # an answering /health/live must not make a silent worker healthy
    assert main(_cmdline(tmp_path, ("arq",)), url=url, redis_url="redis://127.0.0.1:1/0") == 1


def test_dockerfile_declares_the_healthcheck() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile
    assert '"python", "-m", "gize_api.healthcheck"' in dockerfile


async def test_a_worker_is_healthy_only_while_it_reports_in(
    tmp_path: Path, test_redis_url: str
) -> None:
    """The worker writes Arq's health key every 10 s; a hung worker stops writing it."""
    from redis.asyncio import Redis

    from gize_api.healthcheck import worker_health_key

    key = worker_health_key()

    worker = _cmdline(tmp_path, ("python", "-m", "gize_api.worker"))
    redis = Redis.from_url(test_redis_url)
    try:
        await redis.delete(key)
        assert await asyncio.to_thread(main, worker, redis_url=test_redis_url) == 1
        await redis.set(key, b"j_complete=0", px=5000)
        assert await asyncio.to_thread(main, worker, redis_url=test_redis_url) == 0
    finally:
        await redis.delete(key)
        await redis.aclose()
    assert main(worker, redis_url="redis://127.0.0.1:1/0") == 1  # Redis unreachable


def test_a_migration_container_is_healthy_without_probing(tmp_path: Path) -> None:
    migrate = _cmdline(tmp_path, ("python3", "/opt/venv/bin/alembic", "upgrade", "head"))
    assert main(migrate, url="http://127.0.0.1:1/never", redis_url="redis://127.0.0.1:1/0") == 0
