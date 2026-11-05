"""The only way to call outside services (SMS gateway, Supabase): guarded and bounded.

- **Where:** https only, to hosts on the caller's allowlist (each integration passes its
  own, from its settings), on port 443 unless the entry names another (`host:port`). No
  credentials in URLs, no redirects followed, no proxies or netrc from the environment.
  Callers cannot set framing headers (Host, Content-Length, Transfer-Encoding, ...).
- **Which address:** the host is resolved here and every address must be public (no
  private, loopback, link-local, shared, reserved or multicast ranges, IPv4-mapped forms
  included). The connection goes to the address that was checked, with the real name for
  TLS (certificate checked against it) and the Host header, so DNS cannot swap in a private
  address between the check and the connection. Each host has its own connection pool:
  connections are keyed by address, and two hosts sharing one (a CDN) must each have their
  certificate checked. NAT64 and 6to4 addresses are judged by the IPv4 address they carry.
- **How long:** 3 s to connect, 10 s for the whole call including retries; responses are
  capped at 1 MB, requested and read uncompressed (a compressed body could expand far past
  the cap before it was counted).
- **Retries:** only for methods that are safe to repeat (never POST: an SMS must not be
  sent twice), on connection errors and 502/503/504, at most 3 attempts, with exponential
  backoff and jitter.
- **Never inside a database transaction:** the connection would sit idle in it for the
  whole call (gize_app's idle-in-transaction timeout is 10 s). Commit first, or enqueue a
  job. A call with a transaction open raises OpenTransactionError.
- **Observability:** one CLIENT span and one duration measurement per call, naming the host
  and method, never the path or query (they can carry phone numbers). No trace headers are
  sent: third parties never see our trace IDs.
"""

import asyncio
import ipaddress
import json
import random
import socket
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from opentelemetry.trace import SpanKind, Status, StatusCode

from gize_api.core.db import OpenTransactionError, database_transaction_open
from gize_api.core.telemetry import Telemetry

CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.2
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRY_STATUSES = frozenset({502, 503, 504})
# Set by this client (or httpx) only: a caller's value could duplicate or contradict them.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
        "accept-encoding",
    }
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NEVER_PUBLIC_V6 = (
    ipaddress.ip_network("::/96"),  # IPv4-compatible (deprecated)
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
    ipaddress.ip_network("fec0::/10"),  # site-local (deprecated)
    ipaddress.ip_network("::ffff:0:0:0/96"),  # IPv4-translated (SIIT)
)

Resolver = Callable[[str, int], Awaitable[list[str]]]

__all__ = [
    "BlockedDestinationError",
    "OpenTransactionError",
    "OutboundAnswerLostError",
    "OutboundClient",
    "OutboundError",
    "OutboundResponse",
    "OutboundTimeoutError",
    "OutboundUnavailableError",
    "ResponseTooLargeError",
    "is_public_address",
]

logger = structlog.get_logger("gize_api.http")


class OutboundError(Exception):
    """A call to an outside service did not produce a response. Messages carry no URL."""


class BlockedDestinationError(OutboundError):
    """The URL or the address it resolves to is not allowed."""


class OutboundTimeoutError(OutboundError):
    """No complete response within the time limit."""


class OutboundUnavailableError(OutboundError):
    """The service could not be reached (name, connection or transfer failed)."""


class OutboundAnswerLostError(OutboundUnavailableError):
    """The request went out but no answer came back (read timed out, connection dropped):
    the service may have acted on it. A POST that sends something (an SMS) must treat
    this as "maybe done"; for safe methods it is retried like any unavailability."""


class ResponseTooLargeError(OutboundError):
    """The response exceeded MAX_RESPONSE_BYTES."""


@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: httpx.Headers
    content: bytes

    def json(self) -> Any:  # noqa: ANN401 - JSON
        return json.loads(self.content)


@dataclass(frozen=True)
class _Target:
    host: str
    port: int
    explicit_port: bool
    path: str
    query: str


class OutboundClient:
    def __init__(
        self,
        *,
        allowed_hosts: Collection[str],
        telemetry: Telemetry | None = None,
        resolver: Resolver | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._allowed = frozenset(_allowed_destination(entry) for entry in allowed_hosts)
        self._telemetry = telemetry
        self._resolve = resolver or _resolve
        self._new_transport = transport_factory or (lambda: httpx.AsyncHTTPTransport(retries=0))
        self._clients: dict[tuple[str, int], httpx.AsyncClient] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    def _client_for(self, target: "_Target") -> httpx.AsyncClient:
        """One client, so one connection pool, per destination host."""
        key = (target.host, target.port)
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(
                transport=self._new_transport(),
                timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
            )
        return self._clients[key]

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,  # noqa: ANN401 - any JSON body
        headers: Mapping[str, str] | None = None,
    ) -> OutboundResponse:
        if database_transaction_open():
            raise OpenTransactionError("commit before calling an outside service, or enqueue a job")
        method = method.upper()
        reserved = sorted({name.lower() for name in headers or {}} & RESERVED_HEADERS)
        if reserved:
            raise ValueError(f"headers set by the client, not callers: {', '.join(reserved)}")
        target = self._target(url)
        started = time.perf_counter()
        span = self._start_span(method, target.host)
        status: int | None = None
        error: BaseException | None = None
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                response = await self._attempts(method, target, json, headers)
            status = response.status
            return response
        except TimeoutError as exc:
            error = exc
            raise OutboundTimeoutError("the call did not complete in time") from None
        except OutboundError as exc:
            error = exc
            raise
        finally:
            self._finish(span, method, target.host, started, status, error)

    def _target(self, url: str) -> _Target:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise BlockedDestinationError("not a valid URL") from None
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or (host, port or 443) not in self._allowed
        ):
            self._blocked(host, "not_allowed")
        return _Target(
            host=host,
            port=port or 443,
            explicit_port=port is not None,
            path=parts.path or "/",
            query=parts.query,
        )

    async def _attempts(
        self,
        method: str,
        target: _Target,
        body: Any,  # noqa: ANN401 - any JSON body
        headers: Mapping[str, str] | None,
    ) -> OutboundResponse:
        attempts = MAX_ATTEMPTS if method in SAFE_METHODS else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._send(method, target, body, headers)
            except OutboundUnavailableError:
                if attempt == attempts:
                    raise
            else:
                if response.status not in RETRY_STATUSES or attempt == attempts:
                    return response
            delay = RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            await asyncio.sleep(delay + random.uniform(0, delay))  # noqa: S311 - jitter
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send(
        self,
        method: str,
        target: _Target,
        body: Any,  # noqa: ANN401 - any JSON body
        headers: Mapping[str, str] | None,
    ) -> OutboundResponse:
        address = await self._checked_address(target)
        netloc = f"[{address}]" if address.version == 6 else str(address)
        if target.explicit_port:
            netloc += f":{target.port}"
        request_headers = dict(headers or {})
        request_headers["Host"] = (
            f"{target.host}:{target.port}" if target.explicit_port else target.host
        )
        request_headers["Accept-Encoding"] = "identity"
        try:
            async with self._client_for(target).stream(
                method,
                urlunsplit(("https", netloc, target.path, target.query, "")),
                json=body,
                headers=request_headers,
                extensions={"sni_hostname": target.host},
            ) as response:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    # Answered, but not in a form we read: it may have acted on it.
                    raise OutboundAnswerLostError("the service sent a compressed response")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError("response too large")
                content = bytearray()
                # Uncompressed (checked above), so these bytes are exactly what arrived.
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ResponseTooLargeError("response too large")
                return OutboundResponse(
                    status=response.status_code, headers=response.headers, content=bytes(content)
                )
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            raise OutboundUnavailableError("the service did not answer in time") from None
        except (
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ):
            # Sent, at least in part: the service may have acted on it.
            raise OutboundAnswerLostError("no answer came back from the service") from None
        except httpx.TransportError:
            raise OutboundUnavailableError("the service could not be reached") from None

    async def _checked_address(
        self, target: _Target
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            resolved = await self._resolve(target.host, target.port)
        except OSError:
            raise OutboundUnavailableError("the host name could not be resolved") from None
        addresses = [ipaddress.ip_address(text.split("%", 1)[0]) for text in resolved]
        if not addresses or not all(is_public_address(address) for address in addresses):
            self._blocked(target.host, "not_public")
        return addresses[0]

    def _blocked(self, host: str, reason: str) -> None:
        logger.warning("outbound_request_blocked", host=host[:253], reason=reason)
        raise BlockedDestinationError("destination not allowed")

    def _start_span(self, method: str, host: str) -> Any:  # noqa: ANN401 - a span or None
        if self._telemetry is None:
            return None
        return self._telemetry.tracer_provider.get_tracer("gize_api.http").start_span(
            method,
            kind=SpanKind.CLIENT,
            attributes={"http.request.method": method, "server.address": host},
        )

    def _finish(
        self,
        span: Any,  # noqa: ANN401 - a span or None
        method: str,
        host: str,
        started: float,
        status: int | None,
        error: BaseException | None,
    ) -> None:
        outcome: dict[str, str | int] = {"server.address": host, "http.request.method": method}
        if status is not None:
            outcome["http.response.status_code"] = status
        if error is not None:
            outcome["error.type"] = type(error).__name__
        if error is not None and not isinstance(error, BlockedDestinationError):  # logged
            logger.warning(
                "outbound_request_failed", host=host, method=method, error_type=type(error).__name__
            )
        if span is not None:
            span.set_attributes(outcome)
            if error is not None or (status is not None and status >= 500):
                span.set_status(Status(StatusCode.ERROR))  # no description: no message text
            span.end()
        if self._telemetry is not None:
            self._telemetry.record_outbound_duration(time.perf_counter() - started, outcome)


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable and unicast. IPv6 forms that carry an IPv4 address (mapped, NAT64,
    6to4) are judged by that address; deprecated and local-use forms are never public."""
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in network for network in _NEVER_PUBLIC_V6):
            return False
        embedded = address.ipv4_mapped or address.sixtofour
        if address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            address = embedded
    return address.is_global and not address.is_multicast


def _allowed_destination(entry: str) -> tuple[str, int]:
    """`host` (port 443) or `host:port`, lower-cased; anything else is a configuration bug."""
    try:
        parts = urlsplit(f"//{entry.lower()}")
        port = parts.port
    except ValueError:
        parts, port = None, None
    if (
        parts is None
        or not parts.hostname
        or parts.username is not None
        or parts.netloc != (f"{parts.hostname}:{port}" if port else parts.hostname)
    ):
        raise ValueError(f"allowlist entries are host or host:port, not {entry!r}")
    return parts.hostname, port or 443


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]
