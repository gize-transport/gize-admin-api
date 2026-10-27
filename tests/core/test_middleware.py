"""GZ-003: middleware edge cases, driven at the ASGI level."""

import pytest
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gize_api.core.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from gize_api.main import create_app
from tests.support import SettingsFactory

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


def _must_not_run() -> tuple[list[bool], ASGIApp]:
    calls: list[bool] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(True)

    return calls, inner


def _counting_receiver(*messages: Message) -> tuple[list[int], Receive]:
    calls = [0]
    inner = _receiver(*messages)

    async def receive() -> Message:
        calls[0] += 1
        return await inner()

    return calls, receive


async def test_declared_length_over_limit_rejected_without_reading_body() -> None:
    app_calls, inner = _must_not_run()
    receive_calls, receive = _counting_receiver()
    sent, send = _collector()
    scope = {**HTTP_SCOPE, "headers": [(b"content-length", b"101")]}

    await BodySizeLimitMiddleware(inner, max_bytes=100)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert app_calls == []
    assert receive_calls == [0]


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"10")],  # lies: declares less than it sends
        [(b"content-length", b"10"), (b"content-length", b"5")],  # several values
        [(b"content-length", b"9" * 4400)],  # too many digits for int()
    ],
)
async def test_misleading_content_length_cannot_bypass_limit(
    headers: list[tuple[bytes, bytes]],
) -> None:
    app_calls, inner = _must_not_run()
    sent, send = _collector()
    scope = {**HTTP_SCOPE, "headers": headers}
    receive = _receiver({"type": "http.request", "body": b"x" * 101, "more_body": False})

    await BodySizeLimitMiddleware(inner, max_bytes=100)(scope, receive, send)

    assert sent[0]["status"] == 413
    assert app_calls == []


async def test_body_limit_passes_non_http_scopes_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    _, send = _collector()
    await BodySizeLimitMiddleware(inner, max_bytes=1)({"type": "lifespan"}, _receiver(), send)

    assert seen == ["lifespan"]
