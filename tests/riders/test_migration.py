"""GZ-035: migration 0015's downgrade works once riders have left or moved.

A seat that ran to its end date keeps its status (`active`); the old index (one seat not
`ended` per rider) would then refuse a rider who left and came back, so the downgrade
marks finished seats ended first.
"""

import asyncio
from datetime import date

from sqlalchemy import select

from gize_api.core.db import create_engine, create_sessionmaker
from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.factories import RiderFactory, RouteFactory
from tests.support import DatabaseUrls, SettingsFactory, run_migrations


async def test_the_downgrade_ends_finished_seats_first(
    scratch_database: DatabaseUrls, make_settings: SettingsFactory
) -> None:
    await asyncio.to_thread(run_migrations, scratch_database.migrator, "head")
    engine = create_engine(make_settings(database_url=scratch_database.migrator))
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            rider = await RiderFactory().create(session)
            route = await RouteFactory().create(session)
            for start, end in ((date(2020, 1, 1), date(2020, 1, 31)), (date(2020, 3, 1), None)):
                session.add(
                    RouteAssignment(
                        route_id=route.id,
                        rider_id=rider.id,
                        start_date=start,
                        end_date=end,
                        status=AssignmentStatus.ACTIVE,
                    )
                )
        await asyncio.to_thread(run_migrations, scratch_database.migrator, "0014", downgrade=True)
        async with create_sessionmaker(engine)() as session, session.begin():
            statuses = (
                await session.execute(
                    select(RouteAssignment.start_date, RouteAssignment.status).order_by(
                        RouteAssignment.start_date
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    assert [status for _, status in statuses] == ["ended", "active"]
