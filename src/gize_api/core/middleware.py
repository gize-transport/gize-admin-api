"""Pure ASGI middleware: request body size limit and security headers."""

from starlette.datastructures import MutableHeaders
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

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit():
                return int(value)
        return 0


class SecurityHeadersMiddleware:
    """Add headers every response must carry, including errors raised by other middleware."""

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
