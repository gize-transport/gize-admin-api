"""Area request and response bodies."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gize_api.core.pagination import Page, PageParams
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText


class AreaWrite(BaseModel):
    """An area's name: trimmed, kept as typed, unique ignoring case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ShortText


class AreaRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    created_at: datetime
    updated_at: datetime


class AreaPage(Page[AreaRead]):
    """A page of areas (a named schema: generated clients keep the name)."""


class AreaListParams(PageParams):
    """`q`: names starting with it, ignoring case (for a picker as staff type). Blank means
    no filter: a picker may send its input box as it is."""

    q: ShortText | None = None

    @field_validator("q", mode="before")
    @classmethod
    def _blank_is_no_filter(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value
