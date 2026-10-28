"""The per-server lock that keeps concurrent test runs from sharing database roles."""

import fcntl
from pathlib import Path

import pytest

from tests.support import hold_server_lock, server_lock_path


def test_one_lock_per_server_however_it_is_named() -> None:
    assert server_lock_path("postgresql://postgres:pw@localhost:5434/postgres") == (
        server_lock_path("postgresql://other:pw@127.0.0.1:5434/postgres")
    )
    assert server_lock_path("postgresql://p:pw@127.0.0.1:5434/x") != (
        server_lock_path("postgresql://p:pw@127.0.0.1:5433/x")
    )


def test_a_held_lock_fails_with_a_message_after_the_wait(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    with path.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with (
            pytest.raises(pytest.fail.Exception, match="another test run"),
            hold_server_lock(path, timeout_seconds=0.3),
        ):
            pass  # pragma: no cover - never reached
    with hold_server_lock(path, timeout_seconds=0.3):
        pass  # free again
