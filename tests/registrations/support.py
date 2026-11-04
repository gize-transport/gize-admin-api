"""Registration test helpers (GZ-032): a trip, routes on it, verified phones."""

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.routes.models import RouteArea, RouteStatus
from gize_api.verification.models import PhoneVerification
from gize_api.verification.tokens import issue_verified_phone_token
from tests.factories import AreaFactory, CorridorFactory, RouteAssignmentFactory, RouteFactory
from tests.verification.support import TOKEN_KEY

ADULT = "/v1/registrations/adult"
PHONE = "+251911234567"
PRICED = {"price_round_trip": 3000, "price_one_way": 1800}
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
_minutes = itertools.count()


def later() -> datetime:
    """Rows made in one test share the transaction's clock: order them explicitly."""
    return START + timedelta(minutes=next(_minutes))


@dataclass(frozen=True)
class Trip:
    """Bole (A) to Mexico (B), passing Megenagna; Summit is elsewhere."""

    bole: uuid.UUID
    megenagna: uuid.UUID
    mexico: uuid.UUID
    summit: uuid.UUID
    corridor: uuid.UUID


async def add_trip(session: AsyncSession) -> Trip:
    async with session.begin():
        bole, megenagna, mexico, summit = [
            (await AreaFactory().create(session, name=name)).id
            for name in ("Bole", "Megenagna", "Mexico", "Summit")
        ]
        corridor = await CorridorFactory().create(
            session, origin_area_id=bole, destination_area_id=mexico
        )
    return Trip(bole, megenagna, mexico, summit, corridor=corridor.id)


async def add_route(
    session: AsyncSession,
    trip: Trip,
    *,
    serves: tuple[uuid.UUID, ...] = (),
    **values: Any,  # noqa: ANN401
) -> uuid.UUID:
    """A public route on the trip's corridor, forming with prices unless told otherwise."""
    async with session.begin():
        route = await RouteFactory().create(
            session,
            **{
                "corridor_id": trip.corridor,
                "status": RouteStatus.FORMING,
                "created_at": later(),
                **PRICED,
                **values,
            },
        )
        for position, area_id in enumerate(serves, start=1):
            session.add(RouteArea(route_id=route.id, area_id=area_id, position=position))
        return route.id


async def take_seats(session: AsyncSession, route_id: uuid.UUID, count: int) -> None:
    async with session.begin():
        for _ in range(count):
            await RouteAssignmentFactory().create(session, route_id=route_id)


async def verified(
    session: AsyncSession, phone: str = PHONE, *, confirmed: bool = True, token_phone: str = ""
) -> str:
    """A token for a phone whose code was confirmed (as GZ-031's /verify returns it)."""
    verification_id = uuid.uuid4()
    async with session.begin():
        session.add(
            PhoneVerification(
                id=verification_id,
                phone=phone,
                code_hash="0" * 64,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                verified_at=datetime.now(UTC) if confirmed else None,
            )
        )
    token, _ = issue_verified_phone_token(token_phone or phone, verification_id, key=TOKEN_KEY)
    return token


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def body(token: str, trip: Trip, **changes: object) -> dict[str, object]:
    """A sign-up from Bole to Mexico, round trip, no Saturday, no route chosen."""
    values: dict[str, object] = {
        "verified_phone_token": token,
        "full_name": "Abebe Kebede",
        "home_area_id": str(trip.bole),
        "destination_area_id": str(trip.mexico),
        "plan": "round_trip",
        **changes,
    }
    return {name: value for name, value in values.items() if value is not None}
