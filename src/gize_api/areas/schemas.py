"""Area request and response bodies."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.pagination import NameSortedParams, Page
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, SearchText, ShortText


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


class AreaListParams(NameSortedParams):
    """`q`: names starting with it, ignoring case (for a picker as staff type). Blank means
    no filter."""

    q: SearchText = None
