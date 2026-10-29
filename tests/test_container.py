"""Guards for the container entrypoint (graceful shutdown, server flags)."""

from pathlib import Path

ENTRYPOINT = (Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh").read_text()


def test_api_uses_the_app_factory() -> None:
    assert "uvicorn gize_api.main:create_app" in ENTRYPOINT
    assert "--factory" in ENTRYPOINT


def test_graceful_shutdown_is_30_seconds() -> None:
    assert "--timeout-graceful-shutdown 30" in ENTRYPOINT


def test_lifespan_failure_is_fatal_and_websockets_off() -> None:
    assert "--lifespan on" in ENTRYPOINT
    assert "--ws none" in ENTRYPOINT


def test_proxy_headers_and_server_header_off() -> None:
    assert "--no-proxy-headers" in ENTRYPOINT
    assert "--no-server-header" in ENTRYPOINT


def test_migrate_command_runs_alembic_as_the_release_step() -> None:
    assert "migrate)" in ENTRYPOINT
    assert "exec alembic upgrade head" in ENTRYPOINT


def test_worker_uses_our_runner_not_arqs_cli() -> None:
    """Arq's CLI would replace the JSON logging with plain text that prints job arguments."""
    assert "exec python -m gize_api.worker" in ENTRYPOINT
    assert "exec arq " not in ENTRYPOINT
