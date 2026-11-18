"""Route test helpers."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.drivers.models import Driver
from tests.factories import (
    AreaFactory,
    CompanyFactory,
    CorridorFactory,
    RouteAssignmentFactory,
    RouteFactory,
)

ROUTES = "/v1/routes"


async def add_route(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    async with session.begin():
        return (await RouteFactory().create(session, **values)).id


async def add_corridor(session: AsyncSession, *names: str) -> uuid.UUID:
    """A corridor between areas with these names (or two new areas)."""
    async with session.begin():
        if not names:
            return (await CorridorFactory().create(session)).id
        origin = await AreaFactory().create(session, name=names[0])
        destination = await AreaFactory().create(session, name=names[1])
        return (
            await CorridorFactory().create(
                session, origin_area_id=origin.id, destination_area_id=destination.id
            )
        ).id


async def add_company(session: AsyncSession, **values: Any) -> uuid.UUID:  # noqa: ANN401
    async with session.begin():
        return (await CompanyFactory().create(session, **values)).id


async def add_driver(session: AsyncSession, full_name: str = "Bereket Tolosa") -> uuid.UUID:
    """An active driver with a unique phone and plate."""
    async with session.begin():
        number = uuid.uuid4().int
        driver = Driver(
            full_name=full_name,
            phone=f"+2519{number % 10**8:08d}",
            address="Bole",
            plate=f"AA 3-{number % 10**5:05d}",
        )
        session.add(driver)
        await session.flush()
        return driver.id


async def add_seat(session: AsyncSession, route_id: uuid.UUID, **values: Any) -> None:  # noqa: ANN401
    async with session.begin():
        await RouteAssignmentFactory().create(session, route_id=route_id, **values)


def route_path(route_id: uuid.UUID) -> str:
    return f"{ROUTES}/{route_id}"
