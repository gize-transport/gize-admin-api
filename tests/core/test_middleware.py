"""GZ-003: middleware edge cases, driven at the ASGI level."""

from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from gize_api.core.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from gize_api.main import create_app
from tests.conftest import SettingsFactory

HTTP_SCOPE: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}


def _receiver(*messages: Message) -> Receive:
    queue = list(messages)

    async def receive() -> Message:
        return queue.pop(0)

    return receive


def _collector() -> tuple[list[Message], Send]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return sent, send


async def test_lifespan_scope_passes_through_all_middleware(
    make_settings: SettingsFactory,
) -> None:
    app: FastAPI = create_app(make_settings())
    sent, send = _collector()
    receive = _receiver({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"})

    await app({"type": "lifespan", "state": {}}, receive, send)

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


async def test_client_disconnect_mid_body_never_reaches_app() -> None:
    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    middleware = BodySizeLimitMiddleware(inner, max_bytes=100)
    sent, send = _collector()
    receive = _receiver(
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.disconnect"},
    )

    await middleware(HTTP_SCOPE, receive, send)

    assert not called
    assert sent == []


async def test_body_replayed_once_then_real_receive_is_used() -> None:
    received: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(await receive())
        received.append(await receive())

    middleware = BodySizeLimitMiddleware(inner, max_bytes=100)
    _, send = _collector()
    receive = _receiver(
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
        {"type": "http.disconnect"},
    )

    await middleware(HTTP_SCOPE, receive, send)

    assert received == [
        {"type": "http.request", "body": b"abcd", "more_body": False},
        {"type": "http.disconnect"},
    ]


async def test_non_numeric_content_length_falls_back_to_counting() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("must not be reached")

    middleware = BodySizeLimitMiddleware(inner, max_bytes=3)
    sent, send = _collector()
    scope = {**HTTP_SCOPE, "headers": [(b"content-length", b"1e9")]}
    receive = _receiver({"type": "http.request", "body": b"abcd", "more_body": False})

    await middleware(scope, receive, send)

    assert sent[0]["status"] == 413


async def test_security_headers_ignore_non_http_messages() -> None:
    async def passthrough(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "websocket.close", "code": 1000})

    sent, send = _collector()
    await SecurityHeadersMiddleware(passthrough)({"type": "websocket"}, _receiver(), send)

    assert sent == [{"type": "websocket.close", "code": 1000}]
