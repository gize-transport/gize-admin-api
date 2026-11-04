"""Rider queries: live (not deleted) riders, the seat each holds, their open requests.

"Holds" means today or later (`routes.repository.holds_a_seat_from`). A rider holds one
seat, or two that follow each other while moving (GZ-035): the old one to the day before
the new one starts.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import cast

from sqlalchemy import Select, and_, exists, func, literal, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from gize_api.core.db import LIKE_ESCAPE, escape_like
from gize_api.riders.models import Rider, RiderType
from gize_api.route_requests.models import OPEN_STATUSES, RouteRequest
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment
from gize_api.routes.repository import holds_a_seat_from

_WITH_AREAS = (
    joinedload(Rider.home_area),
    joinedload(Rider.destination_area),
    joinedload(Rider.company),
)


@dataclass(frozen=True)
class HeldSeat:
    id: uuid.UUID
    route_id: uuid.UUID
    route_code: str
    status: AssignmentStatus
    start_date: date
    end_date: date | None


async def lives_or_goes_to(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live rider's home or destination is this area (indexes on both columns)."""
    statement = exists().where(
        or_(Rider.home_area_id == area_id, Rider.destination_area_id == area_id),
        Rider.not_deleted(),
    )
    return bool(await session.scalar(select(statement)))


async def works_for(session: AsyncSession, company_id: uuid.UUID) -> bool:
    """A live rider belongs to this company (index: ix_riders_company_id)."""
    statement = exists().where(Rider.company_id == company_id, Rider.not_deleted())
    return bool(await session.scalar(select(statement)))


async def get_live_rider(
    session: AsyncSession, rider_id: uuid.UUID, *, for_update: bool = False
) -> Rider | None:
    """The rider with their areas. `for_update` locks the rider row alone first, then
    loads it (re-checking "not deleted" after any wait)."""
    live = (Rider.id == rider_id, Rider.not_deleted())
    if for_update and await session.scalar(select(Rider.id).where(*live).with_for_update()) is None:
        return None
    return await session.scalar(select(Rider).options(*_WITH_AREAS).where(*live))


async def reload(session: AsyncSession, rider: Rider) -> Rider:
    """The rider read again with its areas, after a change of them."""
    statement = (
        select(Rider)
        .options(*_WITH_AREAS)
        .where(Rider.id == rider.id)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(statement)).one()


async def phone_taken(
    session: AsyncSession,
    phone: str,
    *,
    company_id: uuid.UUID | None,
    except_id: uuid.UUID | None,
) -> bool:
    """Another live rider of the same kind has this phone: an adult (`company_id` None,
    `uq_riders_adult_phone`'s rule) or a rider of that company (`uq_riders_company_phone`)."""
    same_kind = (
        Rider.rider_type == RiderType.ADULT
        if company_id is None
        else Rider.company_id == company_id
    )
    statement = exists().where(Rider.phone == phone, same_kind, Rider.not_deleted())
    if except_id is not None:
        statement = statement.where(Rider.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_riders(
    *,
    q: str | None,
    rider_type: RiderType | None,
    seat: AssignmentStatus | None,
    no_seat: bool,
    route_id: uuid.UUID | None,
    today: date,
) -> Select[Rider]:
    """The filtered riders with their areas, unordered: `paginate` orders and bounds."""
    statement = select(Rider).options(*_WITH_AREAS).where(Rider.not_deleted())
    if q is not None:
        statement = statement.where(
            func.lower(Rider.full_name).startswith(
                func.lower(literal(escape_like(q))), escape=LIKE_ESCAPE
            )
        )
    if rider_type is not None:
        statement = statement.where(Rider.rider_type == rider_type)
    held = (RouteAssignment.rider_id == Rider.id, *holds_a_seat_from(today))
    if seat is not None:
        statement = statement.where(exists().where(*held, RouteAssignment.status == seat))
    if no_seat:
        statement = statement.where(not_(exists().where(*held)))
    if route_id is not None:
        statement = statement.where(exists().where(*held, RouteAssignment.route_id == route_id))
    return statement


async def with_phone(session: AsyncSession, phone: str, *, limit: int) -> list[Rider]:
    statement = (
        select(Rider)
        .options(*_WITH_AREAS)
        .where(Rider.phone == phone, Rider.not_deleted())
        .order_by(Rider.created_at.desc(), Rider.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


async def held_seats(
    session: AsyncSession, rider_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, list[HeldSeat]]:
    """The seats each rider holds today or later, earliest start first, with their routes'
    codes, in one query. A rider holding none has no key."""
    if not rider_ids:
        return {}
    rows = await session.execute(
        select(
            RouteAssignment.rider_id,
            RouteAssignment.route_id,
            Route.code,
            RouteAssignment.status,
            RouteAssignment.start_date,
            RouteAssignment.end_date,
            RouteAssignment.id,
        )
        .join(Route, Route.id == RouteAssignment.route_id)
        .where(RouteAssignment.rider_id.in_(rider_ids), *holds_a_seat_from(today))
        .order_by(RouteAssignment.start_date)
    )
    held: dict[uuid.UUID, list[HeldSeat]] = {}
    for rider_id, route_id, code, status, start, end, seat_id in rows:
        # Never null: the query matched it against `rider_ids`.
        held.setdefault(cast(uuid.UUID, rider_id), []).append(
            HeldSeat(seat_id, route_id, code, status, start, end)
        )
    return held


async def open_request_counts(
    session: AsyncSession, rider_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Each rider's live, open route requests, counted in one query."""
    if not rider_ids:
        return {}
    rows = await session.execute(
        select(RouteRequest.rider_id, func.count())
        .where(
            and_(
                RouteRequest.rider_id.in_(rider_ids),
                RouteRequest.not_deleted(),
                RouteRequest.status.in_(OPEN_STATUSES),
            )
        )
        .group_by(RouteRequest.rider_id)
    )
    return {cast(uuid.UUID, rider_id): int(count) for rider_id, count in rows}
