"""Driver rules (GZ-050). Any active staff member adds, lists, reads and edits drivers
(deactivating one too); only admins delete them (soft). Every change is audited, the name,
phone and address redacted."""

import uuid
from typing import NoReturn

from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import audit
from gize_api.core.db import violated_unique_constraint
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.pagination import paginate
from gize_api.core.security import CurrentStaff
from gize_api.drivers import repository
from gize_api.drivers.models import DRIVER_AUDIT, DRIVER_PHONE_UNIQUE, DRIVER_PLATE_UNIQUE, Driver
from gize_api.drivers.schemas import (
    DriverCreate,
    DriverListParams,
    DriverPage,
    DriverRead,
    DriverUpdate,
    plate_form,
)

_TAKEN = {"phone": ErrorCode.DRIVER_EXISTS, "plate": ErrorCode.PLATE_TAKEN}


async def create(
    session: AsyncSession, caller: CurrentStaff, driver: DriverCreate, idempotent: IdempotentRequest
) -> Response:
    """201 with a Location, or the stored response for a retry."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored)
        await _free_or_raise(session, phone=driver.phone, plate=driver.plate, except_id=None)
        row = Driver(
            full_name=driver.full_name,
            phone=driver.phone,
            address=driver.address,
            plate=driver.plate,
        )
        session.add(row)
        await _flush(session)
        await session.refresh(row)
        audit.record(
            session,
            actor_id=caller.id,
            action="driver.created",
            entity=DRIVER_AUDIT,
            entity_id=row.id,
            before=None,
            after=audit.snapshot(DRIVER_AUDIT, row),
        )
        payload = DriverRead.model_validate(row).model_dump(mode="json")
        location = f"/v1/drivers/{row.id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def list_drivers(session: AsyncSession, params: DriverListParams) -> DriverPage:
    """Newest first."""
    q = params.q or None
    async with session.begin():
        page = await paginate(
            session,
            repository.live_drivers(
                q=q, plate_q=None if q is None else plate_form(q), is_active=params.is_active
            ),
            params,
            created_at=Driver.created_at,
            id=Driver.id,
            newest_first=True,
        )
        return DriverPage(
            items=[DriverRead.model_validate(row) for row in page.items],
            next_cursor=page.next_cursor,
        )


async def get(session: AsyncSession, driver_id: uuid.UUID) -> DriverRead:
    async with session.begin():
        return DriverRead.model_validate(await _live(session, driver_id))


async def update(
    session: AsyncSession, caller: CurrentStaff, driver_id: uuid.UUID, change: DriverUpdate
) -> DriverRead:
    """Setting what is already there changes and records nothing."""
    async with session.begin():
        row = await _live(session, driver_id, for_update=True)
        await _free_or_raise(session, phone=change.phone, plate=change.plate, except_id=row.id)
        before = audit.snapshot(DRIVER_AUDIT, row)
        for field in ("full_name", "phone", "address", "plate", "is_active"):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        await _flush(session)
        await session.refresh(row)
        audit.record(
            session,
            actor_id=caller.id,
            action="driver.updated",
            entity=DRIVER_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(DRIVER_AUDIT, row),
        )
        return DriverRead.model_validate(row)


async def delete(session: AsyncSession, caller: CurrentStaff, driver_id: uuid.UUID) -> None:
    """Soft delete (admins: the route's require_role); their phone and plate are free
    again. GZ-053 refuses it while they drive a route."""
    async with session.begin():
        row = await _live(session, driver_id, for_update=True)
        before = audit.snapshot(DRIVER_AUDIT, row)
        row.deleted_at = func.now()  # the database's clock, like every other timestamp
        await session.flush()
        await session.refresh(row, ["deleted_at"])
        audit.record(
            session,
            actor_id=caller.id,
            action="driver.deleted",
            entity=DRIVER_AUDIT,
            entity_id=row.id,
            before=before,
            after=audit.snapshot(DRIVER_AUDIT, row),
        )


async def _free_or_raise(
    session: AsyncSession, *, phone: str | None, plate: str | None, except_id: uuid.UUID | None
) -> None:
    for column, value in (("phone", phone), ("plate", plate)):
        if value is not None and await repository.taken(
            session, column, value, except_id=except_id
        ):
            raise AppError(_TAKEN[column])


async def _live(session: AsyncSession, driver_id: uuid.UUID, *, for_update: bool = False) -> Driver:
    row = await repository.get_live_driver(session, driver_id, for_update=for_update)
    if row is None:  # unknown or deleted: the same answer
        raise AppError(ErrorCode.NOT_FOUND)
    return row


async def _flush(session: AsyncSession) -> None:
    """Flush; a phone or plate another request took meanwhile is the same 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        _taken_or_raise(exc)


def _taken_or_raise(exc: IntegrityError) -> NoReturn:
    index = violated_unique_constraint(exc)
    if index == DRIVER_PHONE_UNIQUE:
        raise AppError(ErrorCode.DRIVER_EXISTS) from None
    if index == DRIVER_PLATE_UNIQUE:
        raise AppError(ErrorCode.PLATE_TAKEN) from None
    raise exc
