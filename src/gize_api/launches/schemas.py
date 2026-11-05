"""Launch message bodies (GZ-041)."""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.schedule import LocalDate
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH
from gize_api.routes.models import MAX_SEATS


class LaunchMessageStatus(StrEnum):
    SENT = "sent"  # the gateway accepted it
    SENDING = "sending"  # being sent right now: retry leaves it alone
    UNCONFIRMED = "unconfirmed"  # sent, but no answer from the gateway: it may have arrived
    FAILED = "failed"  # the latest attempt failed (or stopped midway): retry resends it
    PENDING = "pending"  # not tried yet (the launch's run stopped before it): retry sends it


class LaunchMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rider_id: uuid.UUID
    full_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    start_date: LocalDate = Field(description="Their first day, as the SMS says.")
    status: LaunchMessageStatus
    attempts: int = Field(description="SMS sent or tried for this launch.")


class LaunchMessages(BaseModel):
    """The SMS the route's latest launch sends its riders (one each; never a second once
    one was sent). A route never launched has none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    launch_date: LocalDate | None
    total: int
    sent: int
    sending: int
    unconfirmed: int = Field(description="May have arrived: not resent; call if unsure.")
    failed: int
    pending: int
    riders: list[LaunchMessage] = Field(max_length=MAX_SEATS)
