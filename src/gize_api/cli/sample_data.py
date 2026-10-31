"""Fill a local database with sample data for the admin frontend (GZ-068).

    python -m gize_api.cli.sample_data

Local only: it refuses unless APP_ENV=local, and refuses a database that already has
areas, companies, routes or route requests (even deleted ones), so it never mixes with
data someone typed in. Everything goes through the services, as the API would: the same
rules, and audit entries naming the first active admin (run first_admin first).

Riders come with M3: until then no route has riders, so public and student routes stop at
`forming` and route requests are for new areas (some matched to forming routes).

Each step commits on its own. If one fails, the database keeps the steps before it: rebuild
the local database (README, "Sample data") and run the command again.

Exit codes: 0 done, 1 refused or a dependency failed, 2 usage or settings.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from fastapi import Response
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas import service as areas
from gize_api.areas.models import Area
from gize_api.areas.schemas import AreaWrite
from gize_api.companies import service as companies
from gize_api.companies.models import Company
from gize_api.companies.schemas import CompanyCreate, CompanyUpdate
from gize_api.core.config import ConfigError, Environment, Settings, load_settings
from gize_api.core.db import (
    create_engine,
    create_sessionmaker,
    is_database_misconfigured,
    is_database_unavailable,
)
from gize_api.core.idempotency import IdempotencyKey, IdempotentRequest
from gize_api.core.logging import configure_logging
from gize_api.core.security import CurrentStaff
from gize_api.corridors import service as corridors
from gize_api.corridors.schemas import CorridorCreate
from gize_api.route_requests import service as route_requests
from gize_api.route_requests.models import RouteRequest
from gize_api.route_requests.schemas import RouteRequestCreate, RouteRequestStatusSet
from gize_api.routes import service as routes
from gize_api.routes.models import Route
from gize_api.routes.schemas import (
    RouteAreasSet,
    RouteCopy,
    RouteCreate,
    RoutePricesSet,
    RouteStatusSet,
)
from gize_api.staff.models import Staff, StaffRole

AREAS = ("Ayat", "Bole", "CMC", "Gerji", "Kazanchis", "Megenagna", "Mexico", "Piassa", "Sarbet")
CORRIDORS = (("CMC", "Mexico"), ("Ayat", "Kazanchis"), ("Gerji", "Piassa"), ("Sarbet", "Bole"))
SCHOOL = "Sample Academy"
TIMES = {
    "morning_time": "06:30",
    "morning_arrival_time": "07:20",
    "return_time": "17:30",
    "return_arrival_time": "18:20",
}


class SampleDataRefusedError(Exception):
    """The command will not load sample data here; nothing was changed."""


type _Create[T] = Callable[[AsyncSession, CurrentStaff, T, IdempotentRequest], Awaitable[Response]]


@dataclass(frozen=True)
class _Loader:
    sessions: async_sessionmaker[AsyncSession]
    caller: CurrentStaff

    async def create[T](self, create: _Create[T], body: T) -> uuid.UUID:
        """Create through the service (a fresh key: never a replay); the new row's ID."""
        key = IdempotentRequest(key=IdempotencyKey(uuid.uuid4()), fingerprint=uuid.uuid4().hex)
        async with self.sessions() as session:
            response = await create(session, self.caller, body, key)
        return uuid.UUID(json.loads(bytes(response.body))["id"])


async def load_sample_data(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Raise SampleDataRefusedError without changing anything, or load every step."""
    async with sessions() as session, session.begin():
        has_data = await session.scalar(
            select(
                exists(select(Area.id))
                | exists(select(Company.id))
                | exists(select(Route.id))
                | exists(select(RouteRequest.id))
            )
        )
        if has_data:
            raise SampleDataRefusedError("the database already has data: rebuild it first")
        admin = await session.scalar(
            select(Staff)
            .where(Staff.role == StaffRole.ADMIN, Staff.is_active)
            .order_by(Staff.created_at, Staff.id)
            .limit(1)
        )
        if admin is None:
            raise SampleDataRefusedError("no active admin: run first_admin first")
        caller = CurrentStaff(id=admin.id, role=StaffRole.ADMIN)
    await _load(_Loader(sessions, caller))


async def _load(loader: _Loader) -> None:
    caller, sessions = loader.caller, loader.sessions
    area = {name: await loader.create(areas.create, AreaWrite(name=name)) for name in AREAS}
    corridor = {
        (a, b): await loader.create(
            corridors.create, CorridorCreate(origin_area_id=area[a], destination_area_id=area[b])
        )
        for a, b in CORRIDORS
    }
    company = {
        name: await loader.create(
            companies.create,
            CompanyCreate(
                name=name,
                contact_name=f"Sample Contact {n}",
                contact_phone=f"09110000{n:02d}",
                contact_email=f"transport{n}@example.com",
            ),
        )
        for n, name in enumerate(
            ("Example Engineering PLC", "Example Bank S.C.", "Example Clinic"), start=1
        )
    }
    async with sessions() as session:
        await companies.update(
            session, caller, company["Example Clinic"], CompanyUpdate(is_active=False)
        )

    async def route(code: str, a: str, b: str, **values: object) -> uuid.UUID:
        body = {"code": code, "corridor_id": corridor[(a, b)], **TIMES, "seats": 14, **values}
        if values.get("route_type") != "corporate":
            body.setdefault("launch_minimum", 10)
        return await loader.create(routes.create, RouteCreate.model_validate(body))

    async def priced(route_id: uuid.UUID, round_trip: int, one_way: int, **more: int) -> None:
        async with sessions() as session:
            prices = RoutePricesSet(round_trip=round_trip, one_way=one_way, **more)
            await routes.set_prices(session, caller, route_id, prices)

    async def moved(route_id: uuid.UUID, *statuses: str) -> None:
        for status in statuses:
            async with sessions() as session:
                change = RouteStatusSet.model_validate({"status": status})
                await routes.set_status(session, caller, route_id, change)

    # Public and student routes: not priced (can't form), priced (can), forming.
    await route("CMC-MEX-01", "CMC", "Mexico", route_type="public")
    await priced(await route("CMC-MEX-02", "CMC", "Mexico", route_type="public"), 2400, 1400)
    ayat = await route("AYT-KAZ-01", "Ayat", "Kazanchis", route_type="public")
    await priced(ayat, 2600, 1500, saturday=400)
    async with sessions() as session:
        served = RouteAreasSet(area_ids=[area["Megenagna"]])
        await routes.set_areas(session, caller, ayat, served)
    await moved(ayat, "forming")
    await loader.create(
        lambda session, staff, body, key: routes.copy(session, staff, ayat, body, key),
        RouteCopy(code="AYT-KAZ-02", seats=14, launch_minimum=8),
    )
    school = await route("GRJ-PIA-01", "Gerji", "Piassa", route_type="student", school_name=SCHOOL)
    await priced(school, 3000, 1700)
    await moved(school, "forming")
    # Corporate routes: ready to start, running, paused.
    engineering, bank = company["Example Engineering PLC"], company["Example Bank S.C."]
    await route("EXE-01", "Sarbet", "Bole", route_type="corporate", company_id=engineering)
    running = await route("EXB-01", "Sarbet", "Bole", route_type="corporate", company_id=bank)
    await moved(running, "active")
    paused = await route("EXB-02", "CMC", "Mexico", route_type="corporate", company_id=bank)
    await moved(paused, "active", "paused")

    # Route requests: new areas, open; two matched to forming routes; one declined.
    async def request(n: int, **values: object) -> uuid.UUID:
        body = {
            "kind": "new_areas",
            "rider_type": "adult",
            "rider_name": f"Sample Rider {n}",
            "contact_name": f"Sample Rider {n}",
            "contact_phone": f"09120000{n:02d}",
            **values,
        }
        return await loader.create(route_requests.create, RouteRequestCreate.model_validate(body))

    async def settled(request_id: uuid.UUID, **change: object) -> None:
        async with sessions() as session:
            status = RouteRequestStatusSet.model_validate(change)
            await route_requests.set_status(session, caller, request_id, status)

    await request(1, from_area_name="Lebu", to_area_id=area["Bole"])
    await request(2, from_area_name="Lebu", to_area_id=area["Bole"])
    await request(
        3, from_area_name="Jemo", to_area_id=area["Mexico"],
        preferred_morning_time="06:45", preferred_return_time="17:45",
    )  # fmt: skip
    matched = await request(4, from_area_id=area["Ayat"], to_area_id=area["Kazanchis"])
    await settled(matched, status="matched", matched_route_id=ayat)
    student = await request(
        5, from_area_id=area["Gerji"], to_area_id=area["Piassa"], rider_type="student",
        school_name=SCHOOL, contact_name="Sample Parent 5",
    )  # fmt: skip
    await settled(student, status="matched", matched_route_id=school)
    far = await request(6, from_area_name="Summit", to_area_id=area["Piassa"])
    await settled(far, status="declined", decline_reason="Too far from any corridor for now.")


async def _run(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        await load_sample_data(create_sessionmaker(engine))
    finally:
        await engine.dispose()


def _database_problem(exc: Exception) -> str | None:
    if not is_database_unavailable(exc):
        return None
    if is_database_misconfigured(exc):
        return "the database refused the connection; check DATABASE_URL"
    return "the database is unavailable: rebuild the local database and run it again"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gize_api.cli.sample_data",
        description="Fill an empty local database with sample data (APP_ENV=local only).",
    )
    parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"sample_data: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    if settings.app_env is not Environment.LOCAL:
        print("sample_data: runs only with APP_ENV=local", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    configure_logging(settings.log_level)
    try:
        asyncio.run(_run(settings))
    except SampleDataRefusedError as exc:
        print(f"sample_data: refused: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    except Exception as exc:
        problem = _database_problem(exc)
        if problem is None:
            raise
        print(f"sample_data: {problem}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    print("sample_data: done: 8 routes, 3 companies, 6 route requests", file=sys.stderr)  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":  # pragma: no cover - run from a shell
    sys.exit(main())
