"""The finishing touches that make the OpenAPI document our contract (GZ-015).

FastAPI's schema describes errors as its own: a 422 `HTTPValidationError` in
`application/json`, and nothing for the rest. Ours are RFC 9457 problems (core/errors.py),
so every operation's error responses are rewritten here:

- each 4xx/5xx response carries `application/problem+json` with `Problem` (or
  `ValidationProblem` for VALIDATION_FAILED) and lists its codes in `x-error-codes`;
- 422 VALIDATION_FAILED is documented wherever there is input (parameters or a body),
  413 PAYLOAD_TOO_LARGE wherever there is a body, and 500 INTERNAL_ERROR everywhere;
- `ErrorCode`, `Problem`, `ValidationProblem` and their parts are components, and
  FastAPI's `HTTPValidationError`/`ValidationError` are gone;
- every error response shows one example per code it lists, built from the `Problem` the
  API answers with (GZ-061);
- every 2xx JSON response shows its model's example from `examples` (gize_api/examples.py,
  by component name); a page (`items`, and `next_cursor` if it pages) shows one item;
- the `Idempotency-Key` header is required, a UUID. In the code it is optional, so that a
  missing key is answered with 428 IDEMPOTENCY_KEY_REQUIRED rather than a validation error.

Routes declare the rest with `problem_responses(...)`; any other 4xx/5xx entry (a bare
description, a `model`, `4XX` or `default`) is refused with an error naming the operation.
tests/test_contract.py holds every operation to this shape; `scripts/export_openapi.py`
writes the result to openapi.json.

Not documented (yet): response headers (`WWW-Authenticate`, `Retry-After`, `X-Request-ID`),
and the 413 the body-size middleware can also return for an operation without a body.
Adding them later is an additive change.
"""

from collections.abc import Mapping
from typing import Any

from pydantic.json_schema import models_json_schema

from gize_api.core.errors import (
    ERROR_SPECS,
    ErrorCode,
    FieldError,
    Problem,
    ValidationProblem,
    ValidationReason,
)

REF = "#/components/schemas/{model}"
PROBLEM_REF = REF.format(model="Problem")
VALIDATION_PROBLEM_REF = REF.format(model="ValidationProblem")
PROBLEM_JSON = "application/problem+json"
FASTAPI_ERROR_SCHEMAS = ("HTTPValidationError", "ValidationError")
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
# Error examples carry a request ID, as every real answer does (a made-up one).
EXAMPLE_REQUEST_ID = "4b1f0c3e9a2d4e7f8c6b5a4d3e2f1a0b"


def finish_contract(
    schema: dict[str, Any], examples: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Rewrite FastAPI's schema in place into the contract, and return it. `examples`: a
    JSON example per component name, shown on the 2xx responses that return it."""
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in FASTAPI_ERROR_SCHEMAS:
        components.pop(name, None)
    components.update(_error_schemas())
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                _finish_operation(operation)
                _show_examples(operation, components, examples or {})
    return schema


def _error_schemas() -> dict[str, Any]:
    _, top = models_json_schema(
        [(Problem, "serialization"), (ValidationProblem, "serialization")], ref_template=REF
    )
    return dict(top["$defs"])


def _finish_operation(operation: dict[str, Any]) -> None:
    for parameter in operation.get("parameters", []):
        if parameter.get("in") == "header" and parameter.get("name") == IDEMPOTENCY_KEY_HEADER:
            parameter["required"] = True
            parameter["schema"] = {"type": "string", "format": "uuid"}
            parameter["description"] = (
                "One per user action, reused on every retry of it (api-contract.md)."
            )
    responses: dict[str, Any] = operation.setdefault("responses", {})
    implied: list[ErrorCode] = [ErrorCode.INTERNAL_ERROR]
    if operation.get("parameters") or "requestBody" in operation:
        implied.append(ErrorCode.VALIDATION_FAILED)
    if "requestBody" in operation:
        implied.append(ErrorCode.PAYLOAD_TOO_LARGE)
    for code in implied:
        status = str(ERROR_SPECS[code].status)
        response = responses.setdefault(status, {})
        if "x-error-codes" not in response:
            if response and not _is_fastapi_validation_error(response):
                continue  # written by hand: refused below
            response.clear()
            response["x-error-codes"] = []
        if code.value not in response["x-error-codes"]:
            response["x-error-codes"] = sorted([*response["x-error-codes"], code.value])
    for status, response in responses.items():
        if status.startswith(("1", "2", "3")):
            continue
        if not status.isdigit() or "x-error-codes" not in response or "content" in response:
            raise ValueError(
                f"{operation.get('operationId')}: declare the {status} response with "
                "problem_responses(...): error responses are problem+json with their codes"
            )
        codes = [ErrorCode(code) for code in response["x-error-codes"]]
        response["description"] = ", ".join(sorted({ERROR_SPECS[code].title for code in codes}))
        response["content"] = {PROBLEM_JSON: {"schema": _problem_schema(codes)}}


def _is_fastapi_validation_error(response: dict[str, Any]) -> bool:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return bool(schema.get("$ref") == REF.format(model="HTTPValidationError"))


def _problem_schema(codes: list[ErrorCode]) -> dict[str, Any]:
    if codes == [ErrorCode.VALIDATION_FAILED]:
        return {"$ref": VALIDATION_PROBLEM_REF}
    if ErrorCode.VALIDATION_FAILED in codes:  # e.g. with INVALID_CURSOR: either shape
        return {"anyOf": [{"$ref": VALIDATION_PROBLEM_REF}, {"$ref": PROBLEM_REF}]}
    return {"$ref": PROBLEM_REF}


def _show_examples(
    operation: dict[str, Any], components: dict[str, Any], examples: Mapping[str, Any]
) -> None:
    for status, response in operation["responses"].items():
        if status.startswith("2"):
            media = response.get("content", {}).get("application/json")
            if media is not None and (example := _success_example(media, components, examples)):
                media["example"] = example
        elif status.isdigit() and not status.startswith(("1", "3")):
            response["content"][PROBLEM_JSON]["examples"] = {
                code: _error_example(ErrorCode(code), operation)
                for code in response["x-error-codes"]
            }


def _success_example(
    media: dict[str, Any], components: dict[str, Any], examples: Mapping[str, Any]
) -> Any | None:  # noqa: ANN401 (JSON)
    name = _ref_name(media.get("schema", {}))
    if name is None:
        return None
    if name in examples:
        return examples[name]
    properties = components.get(name, {}).get("properties", {})
    item = _ref_name(properties.get("items", {}).get("items", {}))
    if item is None or item not in examples or not set(properties) <= {"items", "next_cursor"}:
        return None
    page: dict[str, Any] = {"items": [examples[item]]}
    if "next_cursor" in properties:
        page["next_cursor"] = None
    return page


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _error_example(code: ErrorCode, operation: dict[str, Any]) -> dict[str, Any]:
    spec = ERROR_SPECS[code]
    problem: Problem
    if code == ErrorCode.VALIDATION_FAILED:
        problem = ValidationProblem(
            title=spec.title,
            status=spec.status,
            code=code,
            request_id=EXAMPLE_REQUEST_ID,
            errors=[_field_error(operation)],
            errors_truncated=False,
        )
    else:
        problem = Problem(
            title=spec.title, status=spec.status, code=code, request_id=EXAMPLE_REQUEST_ID
        )
    return {"summary": spec.title, "value": problem.model_dump(mode="json")}


def _field_error(operation: dict[str, Any]) -> FieldError:
    """A body's missing field, the first parameter refused, or else an unknown parameter."""
    parameters = operation.get("parameters", [])
    if "requestBody" in operation:
        return FieldError(location="body", field="/name", reason=ValidationReason.MISSING)
    if not parameters:
        return FieldError(location="query", field="/since", reason=ValidationReason.UNKNOWN_FIELD)
    first = parameters[0]
    return FieldError(
        location=first["in"], field=f"/{first['name']}", reason=ValidationReason.INVALID
    )
