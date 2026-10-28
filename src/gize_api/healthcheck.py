"""The image's HEALTHCHECK: `python -m gize_api.healthcheck`.

Asks the API's own `/health/live`, with a short timeout and no proxy. The image also runs
the worker and migrations, which serve no HTTP: when one of those is found among the
container's processes, it reports healthy without probing. Otherwise it probes, whatever
PID 1 is (an init process such as `docker run --init` or ECS's is common). Exit status 0
is healthy, 1 unhealthy (Docker's convention).
"""

import sys
import urllib.error
import urllib.request
from pathlib import Path

LIVE_URL = "http://127.0.0.1:8000/health/live"
TIMEOUT_SECONDS = 2.0
_PROC = Path("/proc")
_COMMANDS_WITHOUT_HTTP = frozenset({"arq", "alembic"})  # the worker and migrate commands


def runs_without_http(proc: Path = _PROC) -> bool:
    """True if a worker or migration process runs in this container."""
    for cmdline in proc.glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue  # the process ended, or is not ours to read
        if any(Path(arg.decode(errors="replace")).name in _COMMANDS_WITHOUT_HTTP for arg in args):
            return True
    return False


def main(proc: Path = _PROC, *, url: str = LIVE_URL, timeout: float = TIMEOUT_SECONDS) -> int:
    if runs_without_http(proc):
        return 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:  # fixed local URL
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":  # pragma: no cover - run by Docker
    sys.exit(main())
