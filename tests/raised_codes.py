"""GZ-061: record every error the API returns while the tests run, with its operation.

tests/conftest.py wraps `GizeAPI.__call__` with `recording` for the whole session, and
tests/test_raised_codes.py's last test compares what was returned with what each operation
documents (`x-error-codes`), so an undocumented code fails the run rather than surprising
the frontend. Only problem+json responses of a matched operation are recorded: a 404 for an
unknown path, a 405, a CORS refusal or a body refused before routing belongs to no operation
(docs/api-contract.md, "Errors any request gets"). Nor is a request with trace headers to
an app with tracing on, whose outer layer routes a copy of the scope (core/telemetry.py).
"""

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi.routing import APIRoute
from starlette.types import Message, Receive, Scope, Send

PROBLEM_JSON = b"application/problem+json"

AppCall = Callable[[Any, Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class Returned:
    method: str
    path: str  # the route's template, as in the contract
    status: int
    code: str


RETURNED: list[Returned] = []


def recording(app_call: AppCall) -> AppCall:
    """`app_call`, recording each problem response a route answered with in RETURNED."""

    async def call(app: Any, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: ANN401
        if scope["type"] != "http":
            await app_call(app, scope, receive, send)
            return
        start: dict[str, Any] = {}
        body = bytearray()

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start["status"] = message["status"]
                start["problem"] = any(
                    name.lower() == b"content-type" and value.startswith(PROBLEM_JSON)
                    for name, value in message.get("headers", [])
                )
            elif message["type"] == "http.response.body" and start.get("problem"):
                body.extend(message.get("body", b""))
            await send(message)

        try:
            await app_call(app, scope, receive, capture)
        finally:
            route = scope.get("route")
            if start.get("problem") and isinstance(route, APIRoute):
                RETURNED.append(
                    Returned(scope["method"], route.path, start["status"], _code(bytes(body)))
                )

    return call


def _code(body: bytes) -> str:
    try:
        return str(json.loads(body)["code"])
    except (ValueError, KeyError, TypeError):
        return "<no code>"


def undocumented(returned: Iterable[Returned], contract: dict[str, Any]) -> list[str]:
    """What was returned that its operation doesn't list. Routes outside the contract (a
    test's own) are skipped; a method the path doesn't have is a 405, not an operation's."""
    found = set()
    for answer in returned:
        operation = contract["paths"].get(answer.path, {}).get(answer.method.lower())
        if operation is None:
            continue
        response = operation["responses"].get(str(answer.status), {})
        if answer.code not in response.get("x-error-codes", []):
            found.add(f"{operation['operationId']}: {answer.status} {answer.code}")
    return sorted(found)


def matched(returned: Iterable[Returned], contract: dict[str, Any]) -> int:
    """How many answers were an operation's: none, after a suite of API tests, means the
    recorder stopped recording."""
    return sum(
        answer.method.lower() in contract["paths"].get(answer.path, {}) for answer in returned
    )
