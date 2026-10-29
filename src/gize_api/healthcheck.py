"""The image's HEALTHCHECK: `python -m gize_api.healthcheck`.

Which container this is comes from its processes, whatever PID 1 is (an init process such
as `docker run --init` or ECS's is common):
- **API** (no worker or migration process found): ask its own `/health/live`, with a short
  timeout and no proxy.
- **Worker:** the worker writes Arq's health key in Redis every 10 s (it expires 1 s
  later); healthy while the key exists, so a hung worker becomes unhealthy.
- **Migration:** a one-off run: healthy.

Exit status 0 is healthy, 1 unhealthy (Docker's convention).
"""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

import redis
from redis.exceptions import RedisError

from gize_api.core.redis_options import connection_options

LIVE_URL = "http://127.0.0.1:8000/health/live"
TIMEOUT_SECONDS = 2.0
WORKER_HEALTH_KEY = "arq:queue:health-check"  # Arq's default queue + its health suffix
_PROC = Path("/proc")
# The worker (`python -m gize_api.worker`, or Arq's CLI) and migrate commands.
_WORKER_COMMANDS = frozenset({"gize_api.worker", "arq"})
_MIGRATE_COMMANDS = frozenset({"alembic"})

Role = Literal["api", "worker", "migrate"]


def container_role(proc: Path = _PROC) -> Role:
    """What this container runs, from its processes (the API unless another is found)."""
    for cmdline in proc.glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue  # the process ended, or is not ours to read
        names = {Path(arg.decode(errors="replace")).name for arg in args}
        if names & _WORKER_COMMANDS:
            return "worker"
        if names & _MIGRATE_COMMANDS:
            return "migrate"
    return "api"


def runs_without_http(proc: Path = _PROC) -> bool:
    """True if a worker or migration process runs in this container."""
    return container_role(proc) != "api"


def worker_reports_in(redis_url: str, *, timeout: float = TIMEOUT_SECONDS) -> bool:
    """True while the worker's health key exists (it renews it every 10 s)."""
    try:
        client = redis.Redis.from_url(
            redis_url,
            **connection_options(
                redis_url, timeout=timeout, max_connections=1, client_name="gize-healthcheck"
            ),
        )
        try:
            return bool(client.exists(WORKER_HEALTH_KEY))
        finally:
            client.close()
    except (RedisError, OSError, ValueError):
        return False


def main(
    proc: Path = _PROC,
    *,
    url: str = LIVE_URL,
    timeout: float = TIMEOUT_SECONDS,
    redis_url: str | None = None,
) -> int:
    role = container_role(proc)
    if role == "migrate":
        return 0
    if role == "worker":
        return 0 if worker_reports_in(redis_url or os.environ.get("REDIS_URL", "")) else 1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:  # fixed local URL
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":  # pragma: no cover - run by Docker
    sys.exit(main())
