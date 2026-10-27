"""Pure ASGI middleware: request body size limit, CORS guard, security headers."""

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


class CORSGuardMiddleware:
    """Tighten Starlette's CORSMiddleware, which it wraps.

    - A disallowed origin gets no CORS headers at all. Starlette never grants it, but still
      adds Expose-Headers (and, on a refused preflight, Allow-Methods/Headers and Max-Age).
    - A refused preflight (400) becomes problem+json instead of Starlette's plain text.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        disallowed = origin not in self.allowed_origins
        preflight = scope["method"] == "OPTIONS" and "access-control-request-method" in headers
        replaced = False

        async def guarded_send(message: Message) -> None:
            nonlocal replaced
            if replaced:
                return  # drop the body of the response we replaced
            if message["type"] == "http.response.start":
                if preflight and message["status"] == 400:
                    replaced = True
                    rejection = problem_response(ErrorCode.CORS_PREFLIGHT_REJECTED)
                    await rejection(scope, receive, send)
                    return
                if disallowed:
                    response_headers = MutableHeaders(scope=message)
                    for name in [n for n in response_headers if n.startswith("access-control-")]:
                        del response_headers[name]
            await send(message)

        await self.app(scope, receive, guarded_send)


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
