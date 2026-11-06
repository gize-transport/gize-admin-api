"""GZ-061: every operation shows the frontend team what it answers, success and error.

Success examples are model instances (gize_api/examples.py), shown on every 2xx response
that returns their model (a page shows one item); error examples are generated from the
same `Problem` the API answers with, one per code the response documents. These tests hold
every operation to that, and every example to the model it illustrates.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from gize_api.core.contract import finish_contract
from gize_api.core.errors import ERROR_SPECS, ErrorCode, Problem, ValidationProblem
from gize_api.examples import EXAMPLES, contract_examples
from gize_api.main import GizeAPI, create_app
from tests.support import ROOT, SettingsFactory

PROBLEM_JSON = "application/problem+json"
# Answered before any operation is chosen (docs/api-contract.md, "Errors any request gets").
FRAMEWORK_CODES = {"BAD_REQUEST", "CORS_PREFLIGHT_REJECTED", "METHOD_NOT_ALLOWED"}


@pytest.fixture
def app(make_settings: SettingsFactory) -> GizeAPI:
    return create_app(make_settings())


def _operations(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{method.upper()} {path}", operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    ]


def _our_models() -> dict[str, list[type[BaseModel]]]:
    """Our models by name: the contract names a response's schema after its model."""
    found: dict[str, list[type[BaseModel]]] = {}
    pending: list[type[BaseModel]] = [BaseModel]
    while pending:
        for model in pending.pop().__subclasses__():
            pending.append(model)
            if model.__module__.startswith("gize_api."):
                found.setdefault(model.__name__, []).append(model)
    return found


def test_every_operation_is_described(app: GizeAPI) -> None:
    for where, operation in _operations(app.openapi()):
        assert operation.get("summary"), where
        assert operation.get("description"), where


def test_every_success_shows_an_example_its_model_answers_with(app: GizeAPI) -> None:
    models = _our_models()
    shown = 0
    for where, operation in _operations(app.openapi()):
        for status, response in operation["responses"].items():
            if not status.startswith("2") or "content" not in response:
                continue
            media = response["content"]["application/json"]
            assert "example" in media, f"{where} {status}: no example"
            [model] = models[media["schema"]["$ref"].rsplit("/", 1)[-1]]
            answer = model.model_validate_json(json.dumps(media["example"]))
            # Exactly what the API would send for it, so dates and enums read as they will.
            assert answer.model_dump(mode="json", by_alias=True) == media["example"], where
            shown += 1
    assert shown >= 59


def test_every_example_is_shown_somewhere(app: GizeAPI) -> None:
    text = json.dumps(app.openapi())
    for name, example in contract_examples().items():
        assert json.dumps(example) in text, f"{name}'s example is shown nowhere"


def test_the_examples_are_one_per_model() -> None:
    names = [type(example).__name__ for example in EXAMPLES]
    assert len(names) == len(set(names))


def test_every_error_shows_an_example_for_each_of_its_codes(app: GizeAPI) -> None:
    for where, operation in _operations(app.openapi()):
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            examples = response["content"][PROBLEM_JSON]["examples"]
            assert set(examples) == set(response["x-error-codes"]), f"{where} {status}"
            for code, example in examples.items():
                assert example["summary"] == ERROR_SPECS[ErrorCode(code)].title
                shape = ValidationProblem if code == ErrorCode.VALIDATION_FAILED else Problem
                problem = shape.model_validate(example["value"])
                assert (problem.code, problem.status) == (code, int(status)), where
                assert problem.request_id, where


def test_a_validation_example_names_a_field() -> None:
    schema = finish_contract(
        {
            "paths": {
                "/v1/a": {"post": {"operationId": "a_create", "requestBody": {}, "responses": {}}}
            }
        }
    )
    response = schema["paths"]["/v1/a"]["post"]["responses"]["422"]
    value = response["content"][PROBLEM_JSON]["examples"]["VALIDATION_FAILED"]["value"]
    assert value["errors"] == [{"location": "body", "field": "/name", "reason": "missing"}]
    assert value["errors_truncated"] is False


def test_a_page_shows_one_item_and_no_next_page() -> None:
    schema = finish_contract(
        {
            "paths": {
                "/v1/a": {
                    "get": {
                        "operationId": "a_list",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/APage"}
                                    }
                                }
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "APage": {
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/ARead"},
                            },
                            "next_cursor": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        }
                    }
                }
            },
        },
        examples={"ARead": {"id": 1}},
    )
    media = schema["paths"]["/v1/a"]["get"]["responses"]["200"]["content"]["application/json"]
    assert media["example"] == {"items": [{"id": 1}], "next_cursor": None}


def test_a_response_without_a_known_example_is_left_alone() -> None:
    """finish_contract shows what it has; the real app is held to having them all above."""
    response = {"content": {"application/json": {"schema": {"type": "array"}}}}
    schema = finish_contract(
        {"paths": {"/v1/a": {"get": {"operationId": "a_list", "responses": {"200": response}}}}},
        examples={"ARead": {"id": 1}},
    )
    media = schema["paths"]["/v1/a"]["get"]["responses"]["200"]["content"]["application/json"]
    assert "example" not in media


def test_only_the_framework_codes_belong_to_no_operation(app: GizeAPI) -> None:
    schema = app.openapi()
    documented = {
        code
        for _, operation in _operations(schema)
        for response in operation["responses"].values()
        for code in response.get("x-error-codes", [])
    }
    assert set(schema["components"]["schemas"]["ErrorCode"]["enum"]) - documented == (
        FRAMEWORK_CODES
    )


def test_the_contract_explains_the_framework_codes() -> None:
    text = Path(ROOT / "docs" / "api-contract.md").read_text()
    for code in FRAMEWORK_CODES:
        assert f"`{code}`" in text, code


def test_a_redirect_shows_no_error_examples() -> None:
    """Only 4xx/5xx responses are problems; a 3xx has no problem body to illustrate."""
    redirect = {"description": "Moved", "headers": {"Location": {"schema": {"type": "string"}}}}
    schema = finish_contract(
        {"paths": {"/v1/a": {"get": {"operationId": "a_get", "responses": {"302": redirect}}}}}
    )
    assert schema["paths"]["/v1/a"]["get"]["responses"]["302"] == redirect
