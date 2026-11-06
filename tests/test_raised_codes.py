"""GZ-061: no undocumented error codes. Every error the API returned anywhere in the test
suite must be listed by its operation (tests/raised_codes.py records them).

tests/conftest.py runs `test_every_error_the_suite_returned_is_documented` last, after
every other test has had its say; run alone, it checks what that run returned.
"""

from typing import Any

import httpx
import pytest

from gize_api.main import create_app
from tests.raised_codes import RETURNED, Returned, matched, undocumented
from tests.support import SettingsFactory

CONTRACT: dict[str, Any] = {
    "paths": {
        "/v1/a/{a_id}": {
            "get": {
                "operationId": "a_get",
                "responses": {
                    "200": {},
                    "404": {"x-error-codes": ["NOT_FOUND"]},
                },
            }
        }
    }
}


def test_a_documented_code_passes() -> None:
    assert undocumented([Returned("GET", "/v1/a/{a_id}", 404, "NOT_FOUND")], CONTRACT) == []


@pytest.mark.parametrize(
    "returned",
    [
        Returned("GET", "/v1/a/{a_id}", 404, "RIDER_EXISTS"),  # a code not listed
        Returned("GET", "/v1/a/{a_id}", 409, "NOT_FOUND"),  # at a status not listed
    ],
)
def test_an_undocumented_code_is_named_with_its_operation(returned: Returned) -> None:
    assert undocumented([returned, returned], CONTRACT) == [
        f"a_get: {returned.status} {returned.code}"
    ]


@pytest.mark.parametrize(
    "returned",
    [
        Returned("GET", "/v1/widgets", 400, "BAD_REQUEST"),  # a test's own route
        Returned("DELETE", "/v1/a/{a_id}", 405, "METHOD_NOT_ALLOWED"),  # no such operation
    ],
)
def test_answers_outside_the_contract_are_not_its_operations(returned: Returned) -> None:
    assert undocumented([returned], CONTRACT) == []


async def test_the_api_s_problems_are_recorded_with_their_route(api: httpx.AsyncClient) -> None:
    before = len(RETURNED)
    assert (await api.get("/v1/overview")).status_code == 401
    assert (await api.get("/v1/nowhere")).status_code == 404  # no route: not recorded
    assert (await api.get("/health/live")).status_code == 200  # not a problem
    assert RETURNED[before:] == [Returned("GET", "/v1/overview", 401, "UNAUTHENTICATED")]


def test_every_error_the_suite_returned_is_documented(
    make_settings: SettingsFactory, request: pytest.FixtureRequest
) -> None:
    # Reordered (`--ff`, `--nf`), it would check before the others had returned anything.
    assert request.session.items[-1] is request.node, "run the suite in its own order"
    contract = create_app(make_settings()).openapi()
    used = (set(getattr(item, "fixturenames", ())) for item in request.session.items)
    if any({"api", "committed_api"} & names for names in used):
        assert matched(RETURNED, contract), "API tests ran, yet nothing was recorded"
    found = undocumented(RETURNED, contract)
    assert not found, "returned but not documented:\n" + "\n".join(found)
