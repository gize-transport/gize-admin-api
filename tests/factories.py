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

from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.core.models import Base
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
