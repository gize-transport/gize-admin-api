"""Audit log queries."""

from sqlalchemy import Select, select

from gize_api.audit_log.schemas import AuditLogParams
from gize_api.core.audit import AuditEntry


def entries(params: AuditLogParams) -> Select[AuditEntry]:
    """The filtered entries, unordered: `paginate` orders and bounds them."""
    statement = select(AuditEntry)
    if params.entity_type is not None:
        statement = statement.where(AuditEntry.entity_type == params.entity_type)
    if params.entity_id is not None:
        statement = statement.where(AuditEntry.entity_id == params.entity_id)
    if params.actor_staff_id is not None:
        statement = statement.where(AuditEntry.actor_staff_id == params.actor_staff_id)
    return statement
