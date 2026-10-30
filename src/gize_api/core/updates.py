"""Partial updates (PATCH bodies): leave a field out to keep it, send at least one, and never
`null` (a field is changed to a value, not emptied by accident).

Subclass `PartialUpdate` and declare every field `X | None = None`. The contract shows each
field as its type only (no null) and asks for at least one.
"""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator


def _without_null(schema: dict[str, Any]) -> None:
    schema["minProperties"] = 1
    for field in schema["properties"].values():  # each one `X | None = None`
        (kept,) = [branch for branch in field.pop("anyOf") if branch != {"type": "null"}]
        field.update(kept)
        field.pop("default")


class PartialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_without_null)

    @model_validator(mode="after")
    def _a_change_without_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("leave a field out to keep it; null is not a value")
        return self
