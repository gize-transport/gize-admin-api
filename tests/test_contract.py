"""GZ-015: the OpenAPI document is the contract with gize-admin-web (docs/api-contract.md).

These tests hold every operation to the contract's shape, so a new endpoint cannot ship
undocumented: a stable operationId, a summary, a response schema, and its errors as our
problem+json with the codes it can return.
"""

import json
import re
import subprocess
import sys
from typing import Any

import pytest
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.contract import finish_contract
from gize_api.core.errors import ERROR_SPECS, ErrorCode, problem_responses
from gize_api.core.security import CurrentStaff, current_staff, staff_router
from gize_api.main import GizeAPI, create_app
from scripts.export_openapi import CONTRACT_PATH, render_contract
from tests.support import ROOT, SettingsFactory


class WidgetError(BaseModel):
    message: str


OPERATION_ID = re.compile(r"[a-z][a-z0-9]*_[a-z][a-z0-9_]*")
PROBLEM_REFS = {"#/components/schemas/Problem", "#/components/schemas/ValidationProblem"}


def _problem_refs(response: dict[str, Any]) -> set[str]:
    schema = response["content"]["application/problem+json"]["schema"]
    refs = [schema] if "$ref" in schema else schema["anyOf"]
    assert refs
    return {ref["$ref"] for ref in refs}


def _operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    ]


@pytest.fixture
def schema(make_settings: SettingsFactory) -> dict[str, Any]:
    return create_app(make_settings()).openapi()


@pytest.fixture
def app_with_inputs(make_settings: SettingsFactory) -> GizeAPI:
    """The app plus a route with a query parameter and a body, as later tickets add."""
    app = create_app(make_settings())
    router = staff_router(prefix="/v1/widgets", tags=["widgets"])

    class WidgetCreate(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str = Field(max_length=50)

    @router.post(
        "",
        operation_id="widgets_create",
        summary="Create a widget",
        responses=problem_responses(ErrorCode.IDEMPOTENCY_KEY_REQUIRED),
    )
    async def create(
        body: WidgetCreate,
        dry_run: bool = Query(default=False),
        caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    ) -> WidgetCreate:
        return body

    @router.get(
        "",
        operation_id="widgets_list",
        summary="List widgets",
        responses=problem_responses(ErrorCode.INVALID_CURSOR),
    )
    async def widgets(cursor: str | None = Query(default=None, max_length=200)) -> list[int]:
        return []

    app.include_router(router)
    return app


def test_every_operation_meets_the_contract(app_with_inputs: GizeAPI) -> None:
    schema = app_with_inputs.openapi()
    operations = _operations(schema)
    assert operations
    ids = [operation["operationId"] for _, _, operation in operations]
    assert len(ids) == len(set(ids)), "operationIds must be unique"
    for method, path, operation in operations:
        where = f"{method} {path}"
        assert OPERATION_ID.fullmatch(operation["operationId"]), where
        assert operation.get("summary"), where
        responses = operation["responses"]
        successes = [status for status in responses if status.startswith("2")]
        assert successes, where
        for status, response in responses.items():
            if status.startswith("2"):
                assert "application/json" in response["content"], where
                continue
            codes = response["x-error-codes"]
            assert codes, f"{where} {status}"
            assert {ERROR_SPECS[ErrorCode(code)].status for code in codes} == {int(status)}
            assert set(response["content"]) == {"application/problem+json"}, f"{where} {status}"
            assert _problem_refs(response) <= PROBLEM_REFS, f"{where} {status}"
        assert "500" in responses, where  # anything can fail


def test_validation_errors_are_our_problem_shape(app_with_inputs: GizeAPI) -> None:
    schema = app_with_inputs.openapi()
    operation = schema["paths"]["/v1/widgets"]["post"]
    invalid = operation["responses"]["422"]
    assert invalid["content"]["application/problem+json"]["schema"]["$ref"] == (
        "#/components/schemas/ValidationProblem"
    )
    assert invalid["x-error-codes"] == ["VALIDATION_FAILED"]
    assert operation["responses"]["413"]["x-error-codes"] == ["PAYLOAD_TOO_LARGE"]
    assert operation["responses"]["428"]["x-error-codes"] == ["IDEMPOTENCY_KEY_REQUIRED"]
    components = schema["components"]["schemas"]
    assert "HTTPValidationError" not in components
    assert "ValidationError" not in components
    assert "HTTPValidationError" not in json.dumps(schema)


def test_codes_at_one_status_are_listed_together(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())
    router = staff_router(prefix="/v1/things")

    @router.get(
        "",
        operation_id="things_list",
        summary="List things",
        responses=problem_responses(ErrorCode.INVALID_CURSOR),
    )
    async def things(cursor: str | None = Query(default=None, max_length=200)) -> list[int]:
        return []

    app.include_router(router)
    responses = app.openapi()["paths"]["/v1/things"]["get"]["responses"]
    assert responses["422"]["x-error-codes"] == ["INVALID_CURSOR", "VALIDATION_FAILED"]


def test_codes_a_route_declares_itself_are_not_repeated(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())
    router = staff_router(prefix="/v1/echo")

    @router.get(
        "",
        operation_id="echo_get",
        summary="Echo",
        responses=problem_responses(ErrorCode.VALIDATION_FAILED, ErrorCode.INTERNAL_ERROR),
    )
    async def echo(text: str = Query(max_length=10)) -> str:
        return text

    app.include_router(router)
    responses = app.openapi()["paths"]["/v1/echo"]["get"]["responses"]
    assert responses["422"]["x-error-codes"] == ["VALIDATION_FAILED"]
    assert responses["500"]["x-error-codes"] == ["INTERNAL_ERROR"]


def test_error_schemas_are_in_the_contract(schema: dict[str, Any]) -> None:
    components = schema["components"]["schemas"]
    assert set(components["ErrorCode"]["enum"]) == {code.value for code in ErrorCode}
    assert components["Problem"]["properties"]["code"] == {"$ref": "#/components/schemas/ErrorCode"}
    assert "errors" in components["ValidationProblem"]["properties"]
    assert {"FieldError", "ValidationReason"} <= set(components)


def test_staff_me_documents_its_errors(schema: dict[str, Any]) -> None:
    responses = schema["paths"]["/v1/staff/me"]["get"]["responses"]
    codes = {status: response.get("x-error-codes") for status, response in responses.items()}
    assert codes == {
        "200": None,
        "401": ["UNAUTHENTICATED"],
        "403": ["FORBIDDEN"],
        "429": ["RATE_LIMITED"],
        "500": ["INTERNAL_ERROR"],
        "503": ["SERVICE_UNAVAILABLE"],
    }


def test_problem_responses_refuses_an_empty_list() -> None:
    with pytest.raises(ValueError, match="code"):
        problem_responses()


def test_the_contract_is_finished_once_and_served_as_finished(
    make_settings: SettingsFactory,
) -> None:
    app = create_app(make_settings())
    first = app.openapi()
    assert app.openapi() is first  # cached: the finishing step never runs twice
    assert "HTTPValidationError" not in json.dumps(first)


def test_the_contract_names_the_api_version(schema: dict[str, Any]) -> None:
    assert schema["info"]["title"] == "Gize Admin API"
    assert schema["info"]["version"] == "v1"  # releases are the api-vX.Y.Z tags


# --- the committed openapi.json ---------------------------------------------------------


def test_the_committed_contract_is_up_to_date() -> None:
    """CI runs the same check; regenerate with `uv run python scripts/export_openapi.py`."""
    assert CONTRACT_PATH == ROOT / "openapi.json"
    assert CONTRACT_PATH.read_text() == render_contract()


def test_the_contract_is_deterministic() -> None:
    first = render_contract()
    assert first == render_contract()
    assert first.endswith("}\n")
    document = json.loads(first)
    assert list(document) == sorted(document)
    assert list(document["paths"]) == sorted(document["paths"])


def test_the_contract_does_not_depend_on_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SUPABASE_URL", "https://zzzzzzzzzzzzzzzzzzzz.supabase.co")
    assert CONTRACT_PATH.read_text() == render_contract()


def _script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(ROOT / "scripts" / "export_openapi.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=ROOT,
    )


def test_the_check_passes_when_up_to_date() -> None:
    result = _script("--check")
    assert result.returncode == 0, result.stderr


def test_the_check_fails_when_stale(tmp_path: Any) -> None:  # noqa: ANN401
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n")
    result = _script("--check", "--output", str(stale))
    assert result.returncode == 1
    assert "out of date" in result.stderr
    assert "scripts/export_openapi.py" in result.stderr


def test_the_script_writes_the_contract(tmp_path: Any) -> None:  # noqa: ANN401
    target = tmp_path / "openapi.json"
    result = _script("--output", str(target))
    assert result.returncode == 0, result.stderr
    assert target.read_text() == render_contract()


@pytest.mark.parametrize(
    "responses",
    [
        {404: {"description": "Not found"}},  # no codes: use problem_responses
        {409: {"description": "Conflict", "model": WidgetError}},  # not our error shape
        {"4XX": {"description": "Some error"}},
        {"default": {"description": "Anything"}},
    ],
)
def test_error_responses_must_be_declared_with_problem_responses(
    make_settings: SettingsFactory, responses: dict[int | str, dict[str, Any]]
) -> None:
    app = create_app(make_settings())
    router = staff_router(prefix="/v1/odd")

    @router.get("", operation_id="odd_get", summary="Odd", responses=responses)
    async def odd() -> list[int]:
        return []

    app.include_router(router)
    with pytest.raises(ValueError, match=r"odd_get.*problem_responses"):
        app.openapi()


def test_problem_type_is_always_present(schema: dict[str, Any]) -> None:
    """It is always sent, so generated clients must not see it as optional."""
    assert "type" in schema["components"]["schemas"]["Problem"]["required"]


def test_path_level_keys_are_left_alone() -> None:
    """OpenAPI allows `parameters` or `summary` beside the operations of a path."""
    shared = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
    schema = {
        "paths": {
            "/v1/x/{id}": {
                "parameters": shared,
                "get": {"operationId": "x_get", "responses": {"200": {"description": "OK"}}},
            }
        }
    }
    finished = finish_contract(schema)
    assert finished["paths"]["/v1/x/{id}"]["parameters"] == shared
    assert set(finished["paths"]["/v1/x/{id}"]["get"]["responses"]) == {"200", "500"}
