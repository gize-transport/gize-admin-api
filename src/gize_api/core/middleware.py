"""Pure ASGI middleware: request body size limit, CORS denial cleanup, security headers."""

from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gize_api.core.errors import ErrorCode, problem_response


class BodySizeLimitMiddleware:
    """Reject request bodies larger than `max_bytes` with 413 before the app sees them.

    The body is buffered (at most `max_bytes`) and replayed to the app, so an oversized
    stream is refused before any handler runs. Files never pass through the API: uploads go
    straight to Storage with signed URLs.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._declared_length(scope) > self.max_bytes:
            await problem_response(ErrorCode.PAYLOAD_TOO_LARGE)(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                return  # client disconnected before sending the whole body
            body: bytes = message.get("body", b"")
            size += len(body)
            if size > self.max_bytes:
                await problem_response(ErrorCode.PAYLOAD_TOO_LARGE)(scope, receive, send)
                return
            chunks.append(body)
            more_body = message.get("more_body", False)

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)

    def _declared_length(self, scope: Scope) -> int:
        """The largest declared Content-Length; 0 if none is usable (the stream is counted)."""
        declared = 0
        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit():
                # A 20+ digit length is over any limit, and int() refuses very long strings.
                length = self.max_bytes + 1 if len(value) > 19 else int(value)
                declared = max(declared, length)
        return declared


class CORSDenialMiddleware:
    """Strip every CORS header from responses to origins that are not allowed.

    Starlette's CORSMiddleware never grants a disallowed origin, but it still adds
    Expose-Headers (and, on a refused preflight, Allow-Methods/Headers and Max-Age). A
    disallowed origin gets no CORS headers at all. Wraps CORSMiddleware.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        origin = Headers(scope=scope).get("origin") if scope["type"] == "http" else None
        if origin is None or origin in self.allowed_origins:
            await self.app(scope, receive, send)
            return

        async def send_without_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name in [n for n in headers if n.startswith("access-control-")]:
                    del headers[name]
            await send(message)

        await self.app(scope, receive, send_without_cors)


class SecurityHeadersMiddleware:
    """Add headers every response must carry.

    Installed outside Starlette's ServerErrorMiddleware (see main.GizeAPI), so unhandled
    errors (500) and responses from other middleware carry them too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, send_with_headers)
