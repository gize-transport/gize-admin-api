"""Staff request and response bodies."""

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from gize_api.core.pagination import Page, PageParams
from gize_api.staff.models import EMAIL_MAX_LENGTH, FULL_NAME_MAX_LENGTH, StaffRole

EMAIL_LOCAL_PART_MAX_LENGTH = 64  # RFC 5321
_NOT_IN_EMAIL = r"@\s\x00-\x1f\x7f-\x9f"
# One "@", a local part, and a domain with a dot; no spaces or control characters. Supabase
# checks the address further (its refusal is a 422 too) and the invite email proves it.
EMAIL_PATTERN = (
    rf"^[^{_NOT_IN_EMAIL}]{{1,{EMAIL_LOCAL_PART_MAX_LENGTH}}}"
    rf"@[^{_NOT_IN_EMAIL}.]+(?:\.[^{_NOT_IN_EMAIL}.]+)+$"
)


def check_full_name(name: str) -> str:
    """A name without control characters (input is trimmed and length-checked first)."""
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("must not contain control characters")
    return name


# Trimmed and lower-case, as gize.staff stores it (its check refuses anything else).
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=EMAIL_MAX_LENGTH, pattern=EMAIL_PATTERN
    ),
]
FullName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=FULL_NAME_MAX_LENGTH),
    AfterValidator(check_full_name),
]


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)  # null is refused: leave the field out instead


class StaffMe(BaseModel):
    """The signed-in staff member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    full_name: str = Field(max_length=FULL_NAME_MAX_LENGTH)
    email: str = Field(max_length=EMAIL_MAX_LENGTH)
    role: StaffRole


class StaffRead(BaseModel):
    """A staff member as admins see them."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID = Field(description="Their Supabase user ID.")
    full_name: str = Field(max_length=FULL_NAME_MAX_LENGTH)
    email: str = Field(max_length=EMAIL_MAX_LENGTH)
    role: StaffRole
    is_active: bool = Field(description="False: deactivated, locked out until reactivated.")
    created_at: datetime
    updated_at: datetime


class StaffPage(Page[StaffRead]):
    """A page of staff members (a named schema: generated clients keep the name)."""


class StaffListParams(PageParams):
    """Filters, both optional and combined with AND; unknown parameters are refused."""

    role: StaffRole | None = None
    is_active: bool | None = None


class StaffInvite(BaseModel):
    """Who to invite. Supabase emails them a link to set a password; they are active staff
    from the start, and can sign in once they have accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    email: Email
    full_name: FullName
    role: StaffRole


class StaffUpdate(BaseModel):
    """What to change: leave a field out to keep it, and send at least one. Deactivating
    someone locks them out on their next request; `is_active: true` reactivates them."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"minProperties": 1})

    role: StaffRole | SkipJsonSchema[None] = Field(default=None, json_schema_extra=_no_default)
    is_active: StrictBool | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=_no_default
    )

    @model_validator(mode="after")
    def _a_change_without_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send role, is_active or both")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("leave a field out to keep it; null is not a value")
        return self
