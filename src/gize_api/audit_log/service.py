"""Reading the audit log."""

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.audit_log import repository
from gize_api.audit_log.schemas import AuditEntryPage, AuditEntryRead, AuditLogParams
from gize_api.core.audit import AuditEntry
from gize_api.core.pagination import paginate


async def list_entries(session: AsyncSession, params: AuditLogParams) -> AuditEntryPage:
    """Newest first. Callers are admins (the route's require_role)."""
    async with session.begin():
        page = await paginate(
            session,
            repository.entries(params),
            params,
            created_at=AuditEntry.created_at,
            id=AuditEntry.id,
            newest_first=True,
        )
        return AuditEntryPage(
            items=[AuditEntryRead.model_validate(entry) for entry in page.items],
            next_cursor=page.next_cursor,
        )
