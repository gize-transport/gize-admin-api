"""The image's HEALTHCHECK: `python -m gize_api.healthcheck`.

Asks the API's own `/health/live`, with a short timeout and no proxy. The image also runs
the worker and migrations, which serve no HTTP; there it reports healthy without probing.
Exit status 0 is healthy, 1 unhealthy (Docker's convention).
"""

import sys
import urllib.error
import urllib.request
from pathlib import Path

LIVE_URL = "http://127.0.0.1:8000/health/live"
TIMEOUT_SECONDS = 2.0
_PROCESS_CMDLINE = Path("/proc/1/cmdline")  # the container's main process


def is_api_process(cmdline: Path = _PROCESS_CMDLINE) -> bool:
    """True if the main process is uvicorn; also when it cannot be told (probe, not assume)."""
    try:
        args = cmdline.read_bytes().split(b"\0")
    except OSError:
        return True
    return any(Path(arg.decode(errors="replace")).name == "uvicorn" for arg in args)


def main(
    cmdline: Path = _PROCESS_CMDLINE, *, url: str = LIVE_URL, timeout: float = TIMEOUT_SECONDS
) -> int:
    if not is_api_process(cmdline):
        return 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:  # fixed local URL
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":  # pragma: no cover - run by Docker
    sys.exit(main())
