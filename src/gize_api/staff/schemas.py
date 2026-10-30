"""Staff request and response bodies."""

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

from gize_api.core.pagination import Page, PageParams
from gize_api.core.text import no_control_characters
from gize_api.staff.models import EMAIL_MAX_LENGTH, FULL_NAME_MAX_LENGTH, StaffRole

EMAIL_LOCAL_PART_MAX_LENGTH = 64  # RFC 5321
# Punctuation escaped: browsers compile `<input pattern>` with the `v` flag, which refuses
# unescaped `/ { | } -` in a class (tests/staff/test_invite.py runs it under node).
_LOCAL_PART = r"[A-Za-z0-9.!#$%&'*+\/=?^_`\{\|\}~\-]"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
# The HTML form rule for email addresses (what Supabase accepts too), ASCII only, with a dot
# in the domain: no spaces, control or invisible characters ever reach Supabase. The
# invite email itself proves the address.
EMAIL_PATTERN = rf"^{_LOCAL_PART}{{1,{EMAIL_LOCAL_PART_MAX_LENGTH}}}@{_LABEL}(?:\.{_LABEL})+$"


def check_full_name(name: str) -> str:
    """A name without control characters (input is trimmed and length-checked first)."""
    return no_control_characters(name)


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


def _without_null(schema: dict[str, Any]) -> None:
    """Optional fields that refuse null (the model validator): the contract shows them as
    their type only, and asks for at least one."""
    schema["minProperties"] = 1
    for field in schema["properties"].values():  # each one `X | None = None`
        (kept,) = [branch for branch in field.pop("anyOf") if branch != {"type": "null"}]
        field.update(kept)
        field.pop("default")


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
    first_signed_in_at: datetime | None = Field(
        description="Null until their first sign-in: an invitation not yet accepted."
    )
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

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_without_null)

    role: StaffRole | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def _a_change_without_nulls(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send role, is_active or both")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("leave a field out to keep it; null is not a value")
        return self
