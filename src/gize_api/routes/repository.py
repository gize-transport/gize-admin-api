"""Route queries: live (not deleted) routes, and the seats their riders hold.

A seat is held on every day from an assignment's start date to its end date unless it has
ended. Counts are per day; a month's count is its busiest day.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Date, Select, cast, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from gize_api.core.db import LIKE_ESCAPE, escape_like
from gize_api.corridors.models import Corridor
from gize_api.routes.models import (
    AssignmentStatus,
    Route,
    RouteArea,
    RouteAssignment,
    RouteStatus,
    RouteType,
)

# The corridor (with both areas) and company in the same query; inner joins where required.
_WITH_PLACES = (
    joinedload(Route.corridor, innerjoin=True).joinedload(Corridor.origin_area, innerjoin=True),
    joinedload(Route.corridor, innerjoin=True).joinedload(
        Corridor.destination_area, innerjoin=True
    ),
    joinedload(Route.company),
    selectinload(Route.route_areas).joinedload(RouteArea.area, innerjoin=True),
)
_HOLDS_A_SEAT = RouteAssignment.status != AssignmentStatus.ENDED


@dataclass(frozen=True)
class SeatCounts:
    this_month: int = 0
    next_month: int = 0
    reserved: int = 0


async def get_live_route(
    session: AsyncSession, route_id: uuid.UUID, *, for_update: bool = False
) -> Route | None:
    """The route with its corridor, areas and company. `for_update` locks the route row
    alone first, then loads it (a locking query with the joins would, after a wait, judge
    the route against places read before the wait; see corridors.repository)."""
    live = (Route.id == route_id, Route.not_deleted())
    # FOR NO KEY UPDATE: serialises changes to the route (and M3's rider inserts, which
    # take it first) without blocking inserts that only refer to it (its served areas).
    locked = select(Route.id).where(*live).with_for_update(key_share=True)
    if for_update and await session.scalar(locked) is None:
        return None
    return await session.scalar(select(Route).options(*_WITH_PLACES).where(*live))


async def code_taken(session: AsyncSession, code: str, *, except_id: uuid.UUID | None) -> bool:
    statement = exists().where(Route.code == code, Route.not_deleted())
    if except_id is not None:
        statement = statement.where(Route.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_routes(
    *,
    q: str | None,
    route_type: RouteType | None,
    status: RouteStatus | None,
    corridor_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
) -> Select[Route]:
    """The filtered routes with their places, unordered: `paginate` orders and bounds."""
    statement = select(Route).options(*_WITH_PLACES).where(Route.not_deleted())
    if q:
        statement = statement.where(
            Route.code.startswith(func.upper(literal(escape_like(q))), escape=LIKE_ESCAPE)
        )
    for column, value in (
        (Route.route_type, route_type),
        (Route.status, status),
        (Route.corridor_id, corridor_id),
        (Route.company_id, company_id),
    ):
        if value is not None:
            statement = statement.where(column == value)
    return statement


async def seat_counts(
    session: AsyncSession, route_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, SeatCounts]:
    """Seats taken on the busiest day of this month (from today) and of next month, and
    riders reserved for the next launch day (`next_launch_day`: launch progress), for every
    route in two queries."""
    if not route_ids:
        return {}
    next_month = _first_of_next_month(today)
    last_day = _first_of_next_month(next_month) - timedelta(days=1)
    launch_day = next_launch_day(today)
    peaks = {
        route_id: (this_month, next)
        for route_id, this_month, next in await session.execute(
            _peaks(route_ids, today, last_day, split=next_month)
        )
    }
    reserved = {
        route_id: count
        for route_id, count in await session.execute(
            select(RouteAssignment.route_id, func.count())
            .where(
                RouteAssignment.route_id.in_(route_ids),
                RouteAssignment.status == AssignmentStatus.RESERVED,
                RouteAssignment.start_date <= launch_day,
            )
            .group_by(RouteAssignment.route_id)
        )
    }
    return {
        route_id: SeatCounts(*peaks.get(route_id, (0, 0)), reserved=reserved.get(route_id, 0))
        for route_id in route_ids
    }


async def peak_taken_from(session: AsyncSession, route_id: uuid.UUID, today: date) -> int:
    """Seats taken on the busiest day from today on. After the last start date no one
    joins, so that is the last day worth counting."""
    last_start = await session.scalar(
        select(func.max(RouteAssignment.start_date)).where(
            RouteAssignment.route_id == route_id, _HOLDS_A_SEAT
        )
    )
    last_day = max(today, last_start or today)
    row = (await session.execute(_peaks([route_id], today, last_day, split=None))).first()
    return 0 if row is None else int(row[1])


async def has_riders(session: AsyncSession, route_id: uuid.UUID, today: date) -> bool:
    """Someone holds a seat today or later."""
    statement = exists().where(
        RouteAssignment.route_id == route_id,
        _HOLDS_A_SEAT,
        or_(RouteAssignment.end_date.is_(None), RouteAssignment.end_date >= today),
    )
    return bool(await session.scalar(select(statement)))


async def serves_area(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live route serves this area (index: ix_route_areas_area_id)."""
    statement = exists().where(
        RouteArea.area_id == area_id, RouteArea.route_id == Route.id, Route.not_deleted()
    )
    return bool(await session.scalar(select(statement)))


async def uses_corridor(session: AsyncSession, corridor_id: uuid.UUID) -> bool:
    """A live route runs on this corridor (index: ix_routes_corridor_id)."""
    statement = exists().where(Route.corridor_id == corridor_id, Route.not_deleted())
    return bool(await session.scalar(select(statement)))


async def company_runs_a_route(session: AsyncSession, company_id: uuid.UUID) -> bool:
    """One of the company's live routes is active or paused (index: ix_routes_company_id)."""
    statement = exists().where(
        Route.company_id == company_id,
        Route.not_deleted(),
        Route.status.in_((RouteStatus.ACTIVE, RouteStatus.PAUSED)),
    )
    return bool(await session.scalar(select(statement)))


async def company_has_a_route(session: AsyncSession, company_id: uuid.UUID) -> bool:
    statement = exists().where(Route.company_id == company_id, Route.not_deleted())
    return bool(await session.scalar(select(statement)))


def _peaks(
    route_ids: list[uuid.UUID], first_day: date, last_day: date, *, split: date | None
) -> Select[uuid.UUID, int, int]:
    """Per route, the most seats held on any day from `first_day` to `last_day`: before
    `split` and from it (with no split, the whole span, twice). Days nobody holds a seat
    don't appear, so a route with none has no row (0).

    The seats overlapping the span are picked once (a materialised CTE on the route and
    start-date index), then counted per day: the cost follows the span, not the routes'
    whole history (assignments are never deleted).
    """
    overlapping = (
        select(RouteAssignment.route_id, RouteAssignment.start_date, RouteAssignment.end_date)
        .where(
            RouteAssignment.route_id.in_(route_ids),
            _HOLDS_A_SEAT,
            RouteAssignment.start_date <= last_day,
            or_(RouteAssignment.end_date.is_(None), RouteAssignment.end_date >= first_day),
        )
        .cte("overlapping")
        .prefix_with("MATERIALIZED")
    )
    offsets = func.generate_series(0, (last_day - first_day).days).column_valued("offset")
    day = cast(literal(first_day, Date) + offsets, Date)  # date + integer: a date, no zones
    daily = (
        select(overlapping.c.route_id, day.label("day"), func.count().label("taken"))
        .where(
            overlapping.c.start_date <= day,
            or_(overlapping.c.end_date.is_(None), overlapping.c.end_date >= day),
        )
        .group_by(overlapping.c.route_id, day)
        .subquery()
    )
    before = daily.c.day < split if split is not None else literal(True)
    after = daily.c.day >= split if split is not None else literal(True)
    return select(
        daily.c.route_id,
        func.coalesce(func.max(daily.c.taken).filter(before), 0),
        func.coalesce(func.max(daily.c.taken).filter(after), 0),
    ).group_by(daily.c.route_id)


def next_launch_day(today: date) -> date:
    """The earliest day a forming route can launch: today if it's the 1st, else the 1st of
    next month (individual riders start on the 1st). Launch progress counts riders reserved
    from then or earlier, as GZ-024's launch does."""
    return today if today.day == 1 else _first_of_next_month(today)


def _first_of_next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)
