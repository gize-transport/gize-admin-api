"""Audit log query parameters and entries."""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gize_api.core.audit import ACTION_MAX_LENGTH, ENTITY_TYPE_MAX_LENGTH
from gize_api.core.pagination import Page, PageParams


class AuditLogParams(PageParams):
    """Filters, all optional and combined with AND; unknown parameters are refused. An
    entity is its type and ID together (each alone would scan the whole log)."""

    entity_type: str | None = Field(
        default=None, max_length=ENTITY_TYPE_MAX_LENGTH, pattern=r"^[a-z][a-z_]*$"
    )
    entity_id: uuid.UUID | None = None
    actor_staff_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _entity_is_type_and_id(self) -> Self:
        if (self.entity_type is None) != (self.entity_id is None):
            raise ValueError("entity_type and entity_id go together")
        return self


class AuditEntryRead(BaseModel):
    """One recorded change. `before`/`after` hold only the fields that changed; personal
    ones read "[redacted]". `actor_staff_id` is null for operator commands."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    actor_staff_id: uuid.UUID | None
    action: str = Field(max_length=ACTION_MAX_LENGTH)
    entity_type: str = Field(max_length=ENTITY_TYPE_MAX_LENGTH)
    entity_id: uuid.UUID
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None = Field(max_length=64)


class AuditEntryPage(Page[AuditEntryRead]):
    """A page of the audit log (a named schema: generated clients keep the name)."""
