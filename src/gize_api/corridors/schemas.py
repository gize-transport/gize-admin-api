"""Corridor request and response bodies."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gize_api.core.pagination import KeyKind, NameSort, NameSortedParams, Order, Page
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH
from gize_api.core.updates import PartialUpdate


class CorridorCreate(BaseModel):
    """From one area to another (two different areas)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_area_id: uuid.UUID
    destination_area_id: uuid.UUID

    @model_validator(mode="after")
    def _two_areas(self) -> Self:
        if self.origin_area_id == self.destination_area_id:
            raise ValueError("a corridor goes from one area to another")
        return self


class CorridorUpdate(PartialUpdate):
    """Change one end or both; the result must still be two different areas."""

    origin_area_id: uuid.UUID | None = None
    destination_area_id: uuid.UUID | None = None


class AreaRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)


class CorridorRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    origin_area: AreaRef
    destination_area: AreaRef
    created_at: datetime
    updated_at: datetime


class CorridorPage(Page[CorridorRead]):
    """A page of corridors (a named schema: generated clients keep the name)."""


class CorridorListParams(NameSortedParams):
    """Filters, combined with AND; unknown parameters are refused."""

    name_order = Order("name", KeyKind.TEXT, ignore_case=True, parts=2)

    sort: NameSort = Field(
        default=NameSort.NEWEST,
        description="`newest` first, or by `name`: the A area's name A to Z ignoring case, "
        "then the B area's. A cursor pages the sort it was made with: 422 INVALID_CURSOR "
        "with another.",
    )

    origin_area_id: uuid.UUID | None = None
    destination_area_id: uuid.UUID | None = None
