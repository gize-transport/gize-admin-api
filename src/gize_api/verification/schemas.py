"""Phone verification request and response bodies (GZ-031)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gize_api.core.contact import EthiopianMobile

CODE_LENGTH = 6

Code = Annotated[
    str, StringConstraints(min_length=CODE_LENGTH, max_length=CODE_LENGTH, pattern=r"^[0-9]{6}$")
]


class VerificationRequest(BaseModel):
    """The phone to verify, typed any usual way (`0911 23 45 67`); an Ethiopian mobile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phone: EthiopianMobile


class VerificationSent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_id: uuid.UUID
    expires_at: datetime = Field(description="The code works until then (10 minutes).")


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_id: uuid.UUID
    code: Code = Field(description="The six digits from the SMS (ASCII digits only).")


class PhoneVerified(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_phone_token: str = Field(
        max_length=2000,
        description="Present it when signing up, within 30 minutes; it works once.",
    )
    expires_at: datetime
