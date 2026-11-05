"""Rider test helpers (GZ-034, GZ-035)."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.riders.models import Rider
from gize_api.routes.models import AssignmentStatus, RouteAssignment
from tests.factories import RiderFactory
from tests.registrations.support import Trip

RIDERS = "/v1/riders"


async def add_rider(session: AsyncSession, trip: Trip, **values: Any) -> uuid.UUID:  # noqa: ANN401
    """An adult from Bole to Mexico unless told otherwise."""
    async with session.begin():
        return (
            await RiderFactory().create(
                session,
                **{"home_area_id": trip.bole, "destination_area_id": trip.mexico, **values},
            )
        ).id


async def add_seat(
    session: AsyncSession,
    route: uuid.UUID,
    rider: uuid.UUID,
    **values: Any,  # noqa: ANN401
) -> uuid.UUID:
    """A reservation from 1 December unless told otherwise."""
    async with session.begin():
        seat = RouteAssignment(
            **{
                "route_id": route,
                "rider_id": rider,
                "start_date": date(2026, 12, 1),
                "status": AssignmentStatus.RESERVED,
                **values,
            }
        )
        session.add(seat)
        await session.flush()
        return seat.id


async def seats_of(session: AsyncSession, rider: uuid.UUID) -> list[RouteAssignment]:
    """Every seat the rider ever had, oldest start first."""
    async with session.begin():
        statement = (
            select(RouteAssignment)
            .where(RouteAssignment.rider_id == rider)
            .order_by(RouteAssignment.start_date, RouteAssignment.created_at)
        )
        return list((await session.scalars(statement)).all())


async def riders(session: AsyncSession) -> list[Rider]:
    async with session.begin():
        return list((await session.scalars(select(Rider))).all())


def path(rider: uuid.UUID | str) -> str:
    return f"{RIDERS}/{rider}"


def seat_path(rider: uuid.UUID | str) -> str:
    return f"{RIDERS}/{rider}/seat"


def end_path(rider: uuid.UUID | str) -> str:
    return f"{RIDERS}/{rider}/seat/end"
