"""Test data factories: valid rows with unique defaults, any field overridable.

Each domain model gets a subclass naming the model and its defaults:

    class RiderFactory(Factory[Rider]):
        model = Rider

        def defaults(self, sequence: int) -> dict[str, object]:
            return {"phone": f"+2519{sequence:08d}"}

Call `create` inside `async with session.begin():` (the session rule, core/db.py). Rows are
flushed (so server defaults such as `id` are loaded), never committed by the factory: inside
`db_session` they disappear with the test's transaction.
"""

import itertools
import uuid
from datetime import date, time

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.companies.models import Company
from gize_api.core.models import Base
from gize_api.corridors.models import Corridor
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment, RouteType
from gize_api.staff.models import Staff, StaffRole

_sequence = itertools.count(1)


class Factory[M: Base]:
    model: type[M]

    def defaults(self, sequence: int) -> dict[str, object]:
        """Values for one valid row; `sequence` is unique within the test run."""
        return {}

    async def create(self, session: AsyncSession, **overrides: object) -> M:
        values = {**self.defaults(next(_sequence)), **overrides}
        row = self.model(**values)
        session.add(row)
        await session.flush()
        return row

    async def create_batch(self, session: AsyncSession, count: int, **overrides: object) -> list[M]:
        return [await self.create(session, **overrides) for _ in range(count)]


class StaffFactory(Factory[Staff]):
    """Staff members; `id` is the Supabase user ID, so pass the token's `sub` to sign in."""

    model = Staff

    def defaults(self, sequence: int) -> dict[str, object]:
        return {
            "id": uuid.uuid4(),
            "full_name": f"Staff Member {sequence}",
            "email": f"staff{sequence}@example.com",
            "role": StaffRole.STAFF,
        }


class AreaFactory(Factory[Area]):
    model = Area

    def defaults(self, sequence: int) -> dict[str, object]:
        return {"name": f"Area {sequence}"}


class CorridorFactory(Factory[Corridor]):
    """Corridors between two new areas unless `origin_area_id`/`destination_area_id` given."""

    model = Corridor

    async def create(self, session: AsyncSession, **overrides: object) -> Corridor:
        for end in ("origin_area_id", "destination_area_id"):
            if end not in overrides:
                overrides[end] = (await AreaFactory().create(session)).id
        return await super().create(session, **overrides)


class CompanyFactory(Factory[Company]):
    model = Company

    def defaults(self, sequence: int) -> dict[str, object]:
        return {
            "name": f"Company {sequence}",
            "contact_name": f"Contact {sequence}",
            "contact_phone": f"+2519{sequence:08d}",
            "contact_email": f"contact{sequence}@example.com",
        }


class RouteFactory(Factory[Route]):
    """Public routes on a new corridor unless told otherwise. A corporate route needs
    `company_id` and `launch_minimum=None`; a student route `school_name`."""

    model = Route

    def defaults(self, sequence: int) -> dict[str, object]:
        return {
            "code": f"R-{sequence:04d}",
            "route_type": RouteType.PUBLIC,
            "morning_time": time(6, 30),
            "return_time": time(17, 30),
            "seats": 14,
            "launch_minimum": 10,
        }

    async def create(self, session: AsyncSession, **overrides: object) -> Route:
        if "corridor_id" not in overrides:
            overrides["corridor_id"] = (await CorridorFactory().create(session)).id
        return await super().create(session, **overrides)


class RouteAssignmentFactory(Factory[RouteAssignment]):
    """A seat on `route_id`: reserved from 1 December 2026 unless told otherwise."""

    model = RouteAssignment

    def defaults(self, sequence: int) -> dict[str, object]:
        return {"start_date": date(2026, 12, 1), "status": AssignmentStatus.RESERVED}
