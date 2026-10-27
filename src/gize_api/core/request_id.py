"""Request IDs: one per request, on every response and log line, in every error body.

A client may send its own `X-Request-ID` (the admin portal does, so support can match a
screen to our logs). It is kept only if it is 8-64 characters of `[A-Za-z0-9._-]`;
anything else is replaced, so it can never inject into headers or logs.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """The ID of the request being handled, or None outside a request."""
    return _request_id.get()


class RequestIdMiddleware:
    """Assign the request ID before anything else runs and stamp it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _accept_or_generate(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = _request_id.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


def _accept_or_generate(incoming: str | None) -> str:
    if incoming is not None and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex
