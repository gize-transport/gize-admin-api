"""The admin home page's summary (GZ-064): counts only, each one aggregate query (never a
list), each on an index: routes by status and riders over their live rows, held seats on
`ix_route_assignments_held`, offers and requests on their partial indexes. Read committed:
the figures may come from moments a few milliseconds apart."""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import schedule
from gize_api.drivers.models import Driver
from gize_api.offers.models import SeatOffer
from gize_api.overview.schemas import (
    ActiveCount,
    OpenCount,
    Overview,
    RiderCounts,
    RouteCounts,
)
from gize_api.riders.models import Rider
from gize_api.route_requests.models import OPEN_STATUSES, RouteRequest
from gize_api.routes import repository as routes
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment, RouteStatus


async def overview(session: AsyncSession) -> Overview:
    today = schedule.addis_today()
    async with session.begin():
        by_status = {
            status: int(count)
            for status, count in await session.execute(
                select(Route.status, func.count()).where(Route.not_deleted()).group_by(Route.status)
            )
        }
        ready = await routes.ready_to_launch(session, today)
        driverless = await _count(
            session,
            select(func.count()).where(
                Route.not_deleted(),
                Route.status == RouteStatus.ACTIVE,
                Route.driver_id.is_(None),
            ),
        )
        held = (*routes.holds_a_seat_from(today), Rider.not_deleted())
        riders = (
            await session.execute(
                select(
                    func.count(func.distinct(RouteAssignment.rider_id)).filter(
                        RouteAssignment.status == AssignmentStatus.ACTIVE,
                        RouteAssignment.start_date <= today,
                    ),
                    func.count(func.distinct(RouteAssignment.rider_id)).filter(
                        RouteAssignment.status == AssignmentStatus.RESERVED
                    ),
                )
                .join(Rider, Rider.id == RouteAssignment.rider_id)
                .where(*held)
            )
        ).one()
        total_riders = await _count(session, select(func.count()).where(Rider.not_deleted()))
        open_requests = await _count(
            session,
            select(func.count()).where(
                RouteRequest.not_deleted(), RouteRequest.status.in_(OPEN_STATUSES)
            ),
        )
        open_offers = await _count(  # the seats offers hold (GZ-040's rule)
            session, select(func.count()).select_from(SeatOffer).where(*routes.holding_offers())
        )
        active_drivers = await _count(
            session,
            select(func.count()).where(Driver.not_deleted(), Driver.is_active.is_(True)),
        )
    return Overview(
        as_of=today,
        routes=RouteCounts(
            draft=by_status.get(RouteStatus.DRAFT, 0),
            forming=by_status.get(RouteStatus.FORMING, 0),
            active=by_status.get(RouteStatus.ACTIVE, 0),
            paused=by_status.get(RouteStatus.PAUSED, 0),
            ready_to_launch=ready,
            active_without_driver=driverless,
        ),
        riders=RiderCounts(total=total_riders, riding=int(riders[0]), reserved=int(riders[1])),
        route_requests=OpenCount(open=open_requests),
        seat_offers=OpenCount(open=open_offers),
        drivers=ActiveCount(active=active_drivers),
    )


async def _count(session: AsyncSession, statement: Select[int]) -> int:
    return int(await session.scalar(statement) or 0)
