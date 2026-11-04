"""The public catalogue (GZ-036): reads only."""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas import repository as areas
from gize_api.areas.models import Area
from gize_api.catalogue.schemas import (
    PublicAreaListParams,
    PublicAreaPage,
    PublicPrices,
    PublicRoute,
    PublicRouteListParams,
    PublicRoutePage,
)
from gize_api.core import schedule
from gize_api.core.pagination import paginate
from gize_api.corridors.schemas import AreaRef
from gize_api.routes import repository as routes
from gize_api.routes.models import Route
from gize_api.routes.repository import SeatCounts


async def list_areas(session: AsyncSession, params: PublicAreaListParams) -> PublicAreaPage:
    """Oldest first."""
    async with session.begin():
        page = await paginate(
            session, areas.live_areas(q=params.q), params, created_at=Area.created_at, id=Area.id
        )
        return PublicAreaPage(
            items=[AreaRef.model_validate(area) for area in page.items],
            next_cursor=page.next_cursor,
        )


async def list_routes(session: AsyncSession, params: PublicRouteListParams) -> PublicRoutePage:
    """Oldest first, with places and whether they're full (four queries per page)."""
    async with session.begin():
        page = await paginate(
            session,
            routes.public_routes(from_area_id=params.from_area_id, to_area_id=params.to_area_id),
            params,
            created_at=Route.created_at,
            id=Route.id,
        )
        today = schedule.addis_today()
        counts = await routes.seat_counts(session, [route.id for route in page.items], today)
        return PublicRoutePage(
            items=[_read(route, counts[route.id], today) for route in page.items],
            next_cursor=page.next_cursor,
        )


def _read(row: Route, counts: SeatCounts, today: date) -> PublicRoute:
    return PublicRoute(
        id=row.id,
        code=row.code,
        origin_area=AreaRef.model_validate(row.corridor.origin_area),
        destination_area=AreaRef.model_validate(row.corridor.destination_area),
        served_areas=[AreaRef.model_validate(served.area) for served in row.route_areas],
        morning_time=row.morning_time,
        morning_arrival_time=row.morning_arrival_time,
        return_time=row.return_time,
        return_arrival_time=row.return_arrival_time,
        # Listed routes have both prices (`public_routes`); validated all the same.
        prices=PublicPrices.model_validate(
            {
                "round_trip": row.price_round_trip,
                "one_way": row.price_one_way,
                "saturday": row.price_saturday,
            }
        ),
        status=row.status,
        # Judged from the next 1st, the earliest a new rider starts: today's month on the
        # 1st, else next month's busiest day.
        full=(counts.this_month if today.day == 1 else counts.next_month) >= row.seats,
    )
