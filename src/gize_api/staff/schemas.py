"""Staff request and response bodies."""

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

from gize_api.core.contact import EMAIL_MAX_LENGTH, Email
from gize_api.core.pagination import Page, PageParams
from gize_api.core.text import no_control_characters
from gize_api.core.updates import PartialUpdate
from gize_api.staff.models import FULL_NAME_MAX_LENGTH, StaffRole


def check_full_name(name: str) -> str:
    """A name without control characters (input is trimmed and length-checked first)."""
    return no_control_characters(name)


FullName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=FULL_NAME_MAX_LENGTH),
    AfterValidator(check_full_name),
]


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


class StaffUpdate(PartialUpdate):
    """What to change: leave a field out to keep it, and send at least one. Deactivating
    someone locks them out on their next request; `is_active: true` reactivates them. A
    name is trimmed, as in an invite. The email can't change: it is their sign-in."""

    full_name: FullName | None = None
    role: StaffRole | None = None
    is_active: StrictBool | None = None
