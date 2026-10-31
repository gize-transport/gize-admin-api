"""The audit log (GZ-012): who changed what, written in the change's own transaction.

    async with session.begin():
        before = audit.snapshot(STAFF_AUDIT, staff)
        staff.role = StaffRole.ADMIN
        audit.record(session, actor_id=caller.id, action="staff.role_changed",
                     entity=STAFF_AUDIT, entity_id=staff.id,
                     before=before, after=audit.snapshot(STAFF_AUDIT, staff))

- **Same transaction.** `record` refuses to run outside one, so a failed change leaves no
  row and a recorded change cannot be lost.
- **Minimised.** Each entity declares the fields it records and which of them are
  `visible`; the others (names, phones, emails, children's data) are stored as
  "[redacted]": the log shows that they changed, never their values. An empty (null)
  value stays null, which shows only whether the field is set. Before and after must hold
  every declared field (use `snapshot`); an update keeps only the fields that changed, and
  one that changed nothing is not recorded.
- **Who.** `actor_id` is the staff member; an operator command (no staff member) passes
  `actor_id=None, operator=True`, so a forgotten actor fails instead of looking like one.
- **Append-only.** The app role may only SELECT, and INSERT the content columns: the ID
  and time come from the database. UPDATE and DELETE are revoked, and restrictive policies
  make them affect no rows even if granted again by mistake (migration 0004).
"""

import enum
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.models import Base, UUIDPrimaryKeyMixin
from gize_api.core.request_id import current_request_id

REDACTED = "[redacted]"
ACTION = re.compile(r"[a-z][a-z_]*\.[a-z][a-z_]*")
ENTITY_TYPE = re.compile(r"[a-z][a-z_]*")
ACTION_MAX_LENGTH = 64
ENTITY_TYPE_MAX_LENGTH = 50


class AuditEntry(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(r"action ~ '^[a-z][a-z_]*\.[a-z][a-z_]*$'", name="action"),
        CheckConstraint(r"entity_type ~ '^[a-z][a-z_]*$'", name="entity_type"),
        CheckConstraint("before IS NOT NULL OR after IS NOT NULL", name="change"),
        # Keyset pagination, newest first: all rows, one entity's, one actor's.
        Index("ix_audit_log_created_at_id", "created_at", "id"),
        Index(
            "ix_audit_log_entity_type_entity_id_created_at_id",
            "entity_type",
            "entity_id",
            "created_at",
            "id",
        ),
        Index("ix_audit_log_actor_staff_id_created_at_id", "actor_staff_id", "created_at", "id"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # None: not a staff member (an operator command such as the first-admin bootstrap).
    actor_staff_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("gize.staff.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH))
    entity_type: Mapped[str] = mapped_column(String(ENTITY_TYPE_MAX_LENGTH))
    entity_id: Mapped[uuid.UUID]
    # SQL NULL for None (SQLAlchemy would store JSON `null`, which the check cannot see).
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    request_id: Mapped[str | None] = mapped_column(String(64))


@dataclass(frozen=True)
class AuditedEntity:
    """What the log keeps of one kind of record: `fields`, of which `visible` by value."""

    type: str
    fields: tuple[str, ...]
    visible: frozenset[str]

    def __post_init__(self) -> None:
        if not ENTITY_TYPE.fullmatch(self.type) or len(self.type) > ENTITY_TYPE_MAX_LENGTH:
            raise ValueError(f"entity type must be lower_snake_case: {self.type!r}")
        if not self.visible <= set(self.fields):
            raise ValueError(f"visible fields must be among the fields: {sorted(self.visible)}")


def snapshot(entity: AuditedEntity, row: object) -> dict[str, Any]:
    """The audited fields of `row`, for `record`'s before/after."""
    return {field: getattr(row, field) for field in entity.fields}


def record(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    entity: AuditedEntity,
    entity_id: uuid.UUID,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    operator: bool = False,
) -> AuditEntry | None:
    """Add the change's audit row to the open transaction (None if nothing changed)."""
    if not session.in_transaction():
        raise RuntimeError("audit.record runs inside the change's transaction")
    if (actor_id is None) != operator:
        raise ValueError("pass the acting staff member's actor_id, or operator=True for none")
    if not ACTION.fullmatch(action) or len(action) > ACTION_MAX_LENGTH:
        raise ValueError(f"action must look like 'entity.verb': {action!r}")
    if before is None and after is None:
        raise ValueError("a change needs before or after")
    for values in (before, after):
        if values is not None and set(values) != set(entity.fields):
            missing = sorted(set(entity.fields) - set(values))
            unexpected = sorted(set(values) - set(entity.fields))
            raise ValueError(
                f"before/after must hold exactly the fields of {entity.type} (missing:"
                f" {', '.join(missing) or '-'}; unexpected: {', '.join(unexpected) or '-'})"
            )
    if before is not None and after is not None:
        changed = [field for field in entity.fields if before.get(field) != after.get(field)]
        if not changed:
            return None
        before, after = (
            {field: before.get(field) for field in changed},
            {field: after.get(field) for field in changed},
        )
    entry = AuditEntry(
        actor_staff_id=actor_id,
        action=action,
        entity_type=entity.type,
        entity_id=entity_id,
        before=_minimised(entity, before),
        after=_minimised(entity, after),
        request_id=current_request_id(),
    )
    session.add(entry)
    return entry


def _minimised(entity: AuditedEntity, values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    # Every value must be storable, shown or not (a NaN would also always look "changed").
    stored = {field: _json(value) for field, value in values.items()}
    return {
        field: value if field in entity.visible or value is None else REDACTED
        for field, value in stored.items()
    }


def _json(value: object) -> Any:  # noqa: ANN401 - a JSON value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("audit values must be finite numbers (JSON has no NaN or infinity)")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, str):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is None:
        raise ValueError("audit times need a time zone")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, time):  # a schedule time: local, no zone (core/schedule.py)
        return value.isoformat()
    if isinstance(value, list | tuple):  # e.g. the ordered areas a route serves
        return [_json(item) for item in value]
    raise TypeError(f"cannot store a {type(value).__name__} in the audit log as JSON")
