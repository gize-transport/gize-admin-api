"""Giving a rider a seat: a public sign-up's reservation (GZ-032), and staff placing
adults and company riders (GZ-035).

The caller locks the route first (`repository.get_live_route(for_update=True)`), then
calls here: seats are counted and the new one inserted under that lock, so two riders can
never both take the last seat. The lock comes before the insert because the insert's
foreign key takes a share lock on the route: taking the route's lock after it could
deadlock two sign-ups.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.errors import AppError, ErrorCode
from gize_api.routes import repository
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment, RouteStatus


async def reserve(
    session: AsyncSession, route: Route, rider_id: uuid.UUID, today: date
) -> RouteAssignment:
    """A reservation on a forming route (locked by the caller), held from the next 1st,
    the earliest a launch may name: a launch moves it to its own day. 409 ROUTE_FULL when
    a seat isn't free on every day from then."""
    if route.status is not RouteStatus.FORMING:
        raise ValueError("reservations are for forming routes")
    return await take(
        session,
        route,
        rider_id,
        start=repository.next_launch_day(today),
        status=AssignmentStatus.RESERVED,
    )


async def take(
    session: AsyncSession,
    route: Route,
    rider_id: uuid.UUID,
    *,
    start: date,
    status: AssignmentStatus,
    skip_reason: str | None = None,
) -> RouteAssignment:
    """A seat on the route (locked by the caller) from `start`, open-ended. 409 ROUTE_FULL
    when a seat isn't free on every day from then: counted per day, so someone leaving at
    a month's end frees the month after, not that one."""
    if await repository.peak_taken_from(session, route.id, start) >= route.seats:
        raise AppError(ErrorCode.ROUTE_FULL)
    seat = RouteAssignment(
        route_id=route.id,
        rider_id=rider_id,
        start_date=start,
        status=status,
        skip_reason=skip_reason,
    )
    session.add(seat)
    await session.flush()
    return seat
