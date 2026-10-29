"""Staff request and response bodies."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from gize_api.staff.models import EMAIL_MAX_LENGTH, FULL_NAME_MAX_LENGTH, StaffRole


class StaffMe(BaseModel):
    """The signed-in staff member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    full_name: str = Field(max_length=FULL_NAME_MAX_LENGTH)
    email: str = Field(max_length=EMAIL_MAX_LENGTH)
    role: StaffRole
