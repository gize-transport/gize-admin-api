"""Company business rules (GZ-029). Any active staff member manages companies, including
deactivating one; only admins delete them (soft). Every change is audited, with the contact
details and notes redacted. `COMPANY_IN_USE` (GZ-021): a company can't be deactivated while
one of its routes is active or paused, nor deleted while it has any live route."""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.companies import repository
from gize_api.companies.models import COMPANY_AUDIT, COMPANY_NAME_UNIQUE, Company
from gize_api.companies.schemas import (
    CompanyCreate,
    CompanyListParams,
    CompanyPage,
    CompanyRead,
    CompanyUpdate,
)
from gize_api.core import audit
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.riders.repository import works_for as company_has_riders
from gize_api.routes.repository import (
    company_has_a_route,
    company_runs_a_route,
)


async def create(
    session: AsyncSession,
    caller: CurrentStaff,
    company: CompanyCreate,
    idempotent: IdempotentRequest,
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        if await repository.name_taken(session, company.name, except_id=None):
            raise AppError(ErrorCode.COMPANY_NAME_TAKEN)
        row = Company(
            name=company.name,
            contact_name=company.contact_name,
            contact_phone=company.contact_phone,
            contact_email=company.contact_email,
            notes=company.notes or None,
        )
        session.add(row)
        await _flush(session)
        await session.refresh(row, ["billing", "is_active"])
        audit.record(
            session,
            actor_id=caller.id,
            action="company.created",
            entity=COMPANY_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(COMPANY_AUDIT, row),
        )
        payload = CompanyRead.model_validate(row).model_dump(mode="json")
        location = f"/v1/companies/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_companies(session: AsyncSession, params: CompanyListParams) -> CompanyPage:
    """Newest first."""
    async with session.begin():
        page = await paginate(
            session,
            repository.live_companies(q=params.q, is_active=params.is_active),
            params,
            created_at=Company.created_at,
            id=Company.id,
            newest_first=True,
        )
        return CompanyPage(
            items=[CompanyRead.model_validate(company) for company in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, company_id: uuid.UUID) -> CompanyRead:
    async with session.begin():
        return CompanyRead.model_validate(await _live(session, company_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, company_id: uuid.UUID, change: CompanyUpdate
) -> CompanyRead:
    """Setting what is already there changes and records nothing. Activation is recorded as
    its own entry (`company.deactivated`/`company.reactivated`)."""
    async with session.begin():
        row = await _live(session, company_id, lock="update")
        deactivating = row.is_active and change.is_active is False
        if deactivating and await company_runs_a_route(session, row.id):
            raise AppError(ErrorCode.COMPANY_IN_USE)
        if change.name is not None and await repository.name_taken(
            session, change.name, except_id=row.id
        ):
            raise AppError(ErrorCode.COMPANY_NAME_TAKEN)
        before = audit.snapshot(COMPANY_AUDIT, row)
        for field in ("name", "contact_name", "contact_phone", "contact_email"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        if "notes" in change.model_fields_set:
            row.notes = change.notes or None  # empty clears them
        details_changed = audit.snapshot(COMPANY_AUDIT, row)
        if change.is_active is not None:
            row.is_active = change.is_active
        await _flush(session)
        await session.refresh(row, ["updated_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="company.updated",
            entity=COMPANY_AUDIT,
            entity_id=row.id,
            before=before,
            after=details_changed,
        )
        audit.record(
            session,
            actor_id=caller.id,
            action="company.reactivated" if row.is_active else "company.deactivated",
            entity=COMPANY_AUDIT,
            entity_id=row.id,
            before=details_changed,
            after=audit.snapshot(COMPANY_AUDIT, row),
        )
        return CompanyRead.model_validate(row)


async def delete(session: AsyncSession, caller: CurrentStaff, company_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role), refused while it has a live route
    or live riders (GZ-035). The update lock waits for a rider being recorded for it."""
    async with session.begin():
        row = await _live(session, company_id, lock="update")
        if await company_has_a_route(session, row.id) or await company_has_riders(session, row.id):
            raise AppError(ErrorCode.COMPANY_IN_USE)
        before = audit.snapshot(COMPANY_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="company.deleted",
            entity=COMPANY_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(COMPANY_AUDIT, row),
        )


async def _live(
    session: AsyncSession, company_id: uuid.UUID, *, lock: repository.Lock | None = None
) -> Company:
    row = await repository.get_live_company(session, company_id, lock=lock)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _flush(session: AsyncSession) -> None:
    """Flush; a name another request took meanwhile is the same 409 as the check's."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _name_taken_or_raise(exc)


def _name_taken_or_raise(exc: IntegrityError) -> NoReturn:
    if violated_unique_constraint(exc) == COMPANY_NAME_UNIQUE:
        raise AppError(ErrorCode.COMPANY_NAME_TAKEN) from None
    raise exc
