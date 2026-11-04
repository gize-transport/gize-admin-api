"""Giving a rider a seat (GZ-032; GZ-035 extends it to staff and company riders).

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
    start = repository.next_launch_day(today)
    if await repository.peak_taken_from(session, route.id, start) >= route.seats:
        raise AppError(ErrorCode.ROUTE_FULL)
    seat = RouteAssignment(
        route_id=route.id, rider_id=rider_id, start_date=start, status=AssignmentStatus.RESERVED
    )
    session.add(seat)
    await session.flush()
    return seat
