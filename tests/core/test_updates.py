"""PartialUpdate: PATCH bodies that keep what is left out and never accept null."""

import pytest
from pydantic import ValidationError

from gize_api.core.updates import PartialUpdate


class Change(PartialUpdate):
    name: str | None = None
    seats: int | None = None


def test_fields_left_out_are_kept_and_null_is_refused() -> None:
    assert Change.model_validate({"seats": 12}).model_fields_set == {"seats"}
    for body in ({}, {"name": None}, {"seats": 3, "surprise": 1}):
        with pytest.raises(ValidationError):
            Change.model_validate(body)


def test_the_contract_shows_each_field_as_its_type_only() -> None:
    schema = Change.model_json_schema()
    assert schema["minProperties"] == 1
    assert schema["properties"]["seats"] == {"title": "Seats", "type": "integer"}
    assert "required" not in schema


class SeveralTypes(PartialUpdate):
    label: str | int | None = None


class NoDefault(PartialUpdate):
    label: str | None


@pytest.mark.parametrize("model", [SeveralTypes, NoDefault])
def test_a_field_declared_otherwise_is_named(model: type[PartialUpdate]) -> None:
    with pytest.raises(TypeError, match="'label'"):
        model.model_json_schema()
