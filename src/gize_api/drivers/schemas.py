"""Driver bodies (GZ-050)."""

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
)

from gize_api.core.contact import MOBILE_E164_LENGTH, EthiopianMobile
from gize_api.core.pagination import Page, PageParams
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH, ShortText, no_control_characters, same_looking
from gize_api.core.updates import PartialUpdate
from gize_api.drivers.models import ADDRESS_MAX_LENGTH, PLATE_MAX_LENGTH, PLATE_PATTERN

_PLATE = re.compile(PLATE_PATTERN)


def plate_form(value: str) -> str:
    """One spelling per plate: capitals, single spaces ("aa  3-b12345" is "AA 3-B12345")."""
    return " ".join(value.split()).upper()


def _plate_shape(value: str) -> str:
    if not _PLATE.fullmatch(value):
        raise ValueError("letters, digits, spaces and dashes, 2 to 20, e.g. AA 3-B12345")
    return value


Address = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=ADDRESS_MAX_LENGTH),
    AfterValidator(no_control_characters),
    AfterValidator(same_looking),
]
Plate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=PLATE_MAX_LENGTH * 2),
    AfterValidator(plate_form),
    AfterValidator(_plate_shape),
]


class DriverCreate(BaseModel):
    """A driver: the phone typed any usual way (stored `+2519…`), the plate any case or
    spacing (stored in capitals, single spaces)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_name: ShortText
    phone: EthiopianMobile
    address: Address
    plate: Plate = Field(description="The van they drive, e.g. AA 3-B12345.")


class DriverUpdate(PartialUpdate):
    """Change any of these (leave out what stays). `is_active: false` deactivates them."""

    full_name: ShortText | None = None
    phone: EthiopianMobile | None = None
    address: Address | None = None
    plate: Plate | None = None
    is_active: StrictBool | None = None


class DriverRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    full_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    phone: str = Field(max_length=MOBILE_E164_LENGTH, description="E.164, +2519…/+2517…")
    address: str = Field(max_length=ADDRESS_MAX_LENGTH)
    plate: str = Field(max_length=PLATE_MAX_LENGTH)
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DriverPage(Page[DriverRead]):
    """A page of drivers (a named schema: generated clients keep the name)."""


class DriverListParams(PageParams):
    """`q`: names, or plates (any case or spacing), starting with it; blank is no filter."""

    q: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=100)] = None
    is_active: bool | None = None
