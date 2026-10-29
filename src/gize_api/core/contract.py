"""The finishing touches that make the OpenAPI document our contract (GZ-015).

FastAPI's schema describes errors as its own: a 422 `HTTPValidationError` in
`application/json`, and nothing for the rest. Ours are RFC 9457 problems (core/errors.py),
so every operation's error responses are rewritten here:

- each 4xx/5xx response carries `application/problem+json` with `Problem` (or
  `ValidationProblem` for VALIDATION_FAILED) and lists its codes in `x-error-codes`;
- 422 VALIDATION_FAILED is documented wherever there is input (parameters or a body),
  413 PAYLOAD_TOO_LARGE wherever there is a body, and 500 INTERNAL_ERROR everywhere;
- `ErrorCode`, `Problem`, `ValidationProblem` and their parts are components, and
  FastAPI's `HTTPValidationError`/`ValidationError` are gone.

Routes declare the rest with `problem_responses(...)`. tests/test_contract.py holds every
operation to this shape; `scripts/export_openapi.py` writes the result to openapi.json.
"""

from typing import Any

from pydantic.json_schema import models_json_schema

from gize_api.core.errors import ERROR_SPECS, ErrorCode, Problem, ValidationProblem

REF = "#/components/schemas/{model}"
PROBLEM_REF = REF.format(model="Problem")
VALIDATION_PROBLEM_REF = REF.format(model="ValidationProblem")
PROBLEM_JSON = "application/problem+json"
FASTAPI_ERROR_SCHEMAS = ("HTTPValidationError", "ValidationError")


def finish_contract(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite FastAPI's schema in place into the contract, and return it."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in FASTAPI_ERROR_SCHEMAS:
        components.pop(name, None)
    components.update(_error_schemas())
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            _finish_operation(operation)
    return schema


def _error_schemas() -> dict[str, Any]:
    _, top = models_json_schema(
        [(Problem, "serialization"), (ValidationProblem, "serialization")], ref_template=REF
    )
    return dict(top["$defs"])


def _finish_operation(operation: dict[str, Any]) -> None:
    responses: dict[str, Any] = operation.setdefault("responses", {})
    implied: list[ErrorCode] = [ErrorCode.INTERNAL_ERROR]
    if operation.get("parameters") or "requestBody" in operation:
        implied.append(ErrorCode.VALIDATION_FAILED)
    if "requestBody" in operation:
        implied.append(ErrorCode.PAYLOAD_TOO_LARGE)
    for code in implied:
        status = str(ERROR_SPECS[code].status)
        response = responses.setdefault(status, {})
        if "x-error-codes" not in response:  # FastAPI's own 422, or nothing yet
            response.clear()
            response["x-error-codes"] = []
        if code.value not in response["x-error-codes"]:
            response["x-error-codes"] = sorted([*response["x-error-codes"], code.value])
    for status, response in responses.items():
        if not status.startswith(("4", "5")):
            continue
        codes = [ErrorCode(code) for code in response["x-error-codes"]]
        response["description"] = ", ".join(sorted({ERROR_SPECS[code].title for code in codes}))
        response["content"] = {PROBLEM_JSON: {"schema": _problem_schema(codes)}}


def _problem_schema(codes: list[ErrorCode]) -> dict[str, Any]:
    if codes == [ErrorCode.VALIDATION_FAILED]:
        return {"$ref": VALIDATION_PROBLEM_REF}
    if ErrorCode.VALIDATION_FAILED in codes:  # e.g. with INVALID_CURSOR: either shape
        return {"anyOf": [{"$ref": VALIDATION_PROBLEM_REF}, {"$ref": PROBLEM_REF}]}
    return {"$ref": PROBLEM_REF}
