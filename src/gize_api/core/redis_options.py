"""Connection options for every Redis client (API, readiness, worker, container check).

Every client gets timeouts, and a `rediss://` URL also verifies the server's host name:
redis-py 5 (pinned for Arq) does not by default, unlike 6 and later.
"""

from typing import Any
from urllib.parse import urlsplit


def connection_options(
    url: str, *, timeout: float, max_connections: int, client_name: str
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "socket_timeout": timeout,
        "socket_connect_timeout": timeout,
        "max_connections": max_connections,
        "client_name": client_name,
    }
    if urlsplit(url).scheme == "rediss":
        options["ssl_check_hostname"] = True
    return options
