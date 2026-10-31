"""GZ-068: the command that fills a local database with sample data for the frontend.

Local only, on an empty database, through the services (the same rules and audit entries
as the API), as the first active admin. It runs on real, committed rows.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.areas.models import Area
from gize_api.cli import sample_data
from gize_api.cli.sample_data import SampleDataRefusedError, load_sample_data
from gize_api.core.audit import AuditEntry
from gize_api.core.config import ConfigError, Environment
from gize_api.core.errors import AppError, ErrorCode
from gize_api.route_requests.models import RouteRequest
from gize_api.routes.models import Route
from gize_api.staff.models import StaffRole
from tests.factories import AreaFactory, CompanyFactory, RouteRequestFactory, StaffFactory
from tests.staff.support import signed_in
from tests.support import UNREACHABLE_DATABASE_URL, DatabaseUrls, SettingsFactory

DELETED = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)


async def _admin(sessions: async_sessionmaker[AsyncSession], **values: object) -> uuid.UUID:
    async with sessions() as session, session.begin():
        return (await StaffFactory().create(session, role=StaffRole.ADMIN, **values)).id


async def _count(sessions: async_sessionmaker[AsyncSession], model: type[object]) -> int:
    async with sessions() as session, session.begin():
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_it_loads_routes_in_every_state_the_frontend_shows(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    committed_api: httpx.AsyncClient,
) -> None:
    admin_id = await _admin(committed_sessionmaker)
    await load_sample_data(committed_sessionmaker)
    response = await committed_api.get("/v1/routes?limit=50", headers=signed_in(admin_id))
    routes = {route["code"]: route for route in response.json()["items"]}
    assert {code: route["status"] for code, route in routes.items()} == {
        "CMC-MEX-01": "draft",
        "CMC-MEX-02": "draft",
        "AYT-KAZ-01": "forming",
        "AYT-KAZ-02": "forming",
        "GRJ-PIA-01": "forming",
        "EXE-01": "draft",
        "EXB-01": "active",
        "EXB-02": "paused",
    }
    assert routes["CMC-MEX-01"]["status_changes"][0]["blocked_by"] == ["PRICES_MISSING"]
    assert routes["CMC-MEX-02"]["status_changes"][0]["ready"] is True
    assert [area["name"] for area in routes["AYT-KAZ-01"]["served_areas"]] == ["Megenagna"]
    assert routes["AYT-KAZ-02"]["copied_from_route_id"] == routes["AYT-KAZ-01"]["id"]
    assert routes["GRJ-PIA-01"]["school_name"] == "Sample Academy"

    companies = await committed_api.get("/v1/companies", headers=signed_in(admin_id))
    active = {c["name"]: c["is_active"] for c in companies.json()["items"]}
    assert active == {
        "Example Bank S.C.": True,
        "Example Clinic": False,
        "Example Engineering PLC": True,
    }

    summary = await committed_api.get("/v1/route-requests/summary", headers=signed_in(admin_id))
    pairs = {
        (pair["from_area_name"] or pair["from_area"]["name"],
         pair["to_area_name"] or pair["to_area"]["name"]): pair["open"]
        for pair in summary.json()["area_pairs"]
    }  # fmt: skip
    assert pairs == {("Lebu", "Bole"): 2, ("Jemo", "Mexico"): 1}
    by_route = {item["route"]["code"]: item["open"] for item in summary.json()["routes"]}
    assert by_route == {"AYT-KAZ-01": 1, "GRJ-PIA-01": 1}
    requests = await committed_api.get(
        "/v1/route-requests?status=declined", headers=signed_in(admin_id)
    )
    assert len(requests.json()["items"]) == 1


async def test_everything_is_audited_as_the_first_admin(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _admin(committed_sessionmaker)
    await _admin(committed_sessionmaker)  # a later admin
    await _admin(committed_sessionmaker, is_active=False)
    await load_sample_data(committed_sessionmaker)
    async with committed_sessionmaker() as session, session.begin():
        actors = set(await session.scalars(select(AuditEntry.actor_staff_id)))
    assert actors == {first}


@pytest.mark.parametrize("typed_in", ["a deleted area", "a company", "a route request"])
async def test_a_database_with_data_is_refused(
    committed_sessionmaker: async_sessionmaker[AsyncSession], typed_in: str
) -> None:
    """Even deleted rows: sample data never mixes with anything typed in. (Corridors and
    routes need areas, so areas cover them.)"""
    await _admin(committed_sessionmaker)
    async with committed_sessionmaker() as session, session.begin():
        if typed_in == "a deleted area":
            await AreaFactory().create(session, name="Typed in", deleted_at=DELETED)
        elif typed_in == "a company":
            await CompanyFactory().create(session)
        else:
            await RouteRequestFactory().create(session)
    with pytest.raises(SampleDataRefusedError, match="already has data"):
        await load_sample_data(committed_sessionmaker)
    assert await _count(committed_sessionmaker, Route) == 0


async def test_it_needs_an_active_admin(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _admin(committed_sessionmaker, is_active=False)
    async with committed_sessionmaker() as session, session.begin():
        await StaffFactory().create(session, role=StaffRole.STAFF)
    with pytest.raises(SampleDataRefusedError, match="first_admin"):
        await load_sample_data(committed_sessionmaker)
    assert await _count(committed_sessionmaker, Area) == 0


# The command


@pytest.fixture
def local_settings(
    monkeypatch: pytest.MonkeyPatch, make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    settings = make_settings(app_env=Environment.LOCAL, database_url=database.app)
    monkeypatch.setattr(sample_data, "load_settings", lambda: settings)


async def _main(*argv: str) -> int:
    return await asyncio.to_thread(sample_data.main, list(argv))


async def test_the_command_loads_the_sample_data(
    local_settings: None,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _admin(committed_sessionmaker)
    assert await _main() == 0
    assert await _count(committed_sessionmaker, Route) == 8
    assert await _count(committed_sessionmaker, RouteRequest) == 6
    assert "sample_data: done" in capsys.readouterr().err


async def test_the_command_reports_a_refusal(
    local_settings: None,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await _main() == 1  # no admin yet
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize("app_env", [Environment.TEST, Environment.STAGING, Environment.PRODUCTION])
async def test_the_command_runs_only_locally(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    app_env: Environment,
) -> None:
    await _admin(committed_sessionmaker)
    # Staging and production settings need TLS to the database: skip their validation.
    settings = make_settings(database_url=database.app).model_copy(update={"app_env": app_env})
    monkeypatch.setattr(sample_data, "load_settings", lambda: settings)
    assert await _main() == 2
    assert "APP_ENV=local" in capsys.readouterr().err
    assert await _count(committed_sessionmaker, Area) == 0


async def test_bad_settings_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise ConfigError("DATABASE_URL: field required")

    monkeypatch.setattr(sample_data, "load_settings", broken)
    assert await _main() == 2
    assert "DATABASE_URL" in capsys.readouterr().err


async def test_arguments_are_refused(local_settings: None) -> None:
    with pytest.raises(SystemExit) as exc_info:
        await _main("--production")
    assert exc_info.value.code == 2


async def test_the_command_reports_the_database_down(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(app_env=Environment.LOCAL, database_url=UNREACHABLE_DATABASE_URL)
    monkeypatch.setattr(sample_data, "load_settings", lambda: settings)
    assert await _main() == 1
    assert "database is unavailable" in capsys.readouterr().err


async def test_other_errors_are_not_hidden(
    local_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(sessions: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(sample_data, "load_sample_data", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        await _main()


async def test_wrong_database_credentials_say_so(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parts = urlsplit(database.app)
    wrong = urlunsplit(parts._replace(netloc=f"gize_app:wrong@{parts.hostname}:{parts.port}"))
    settings = make_settings(app_env=Environment.LOCAL, database_url=wrong)
    monkeypatch.setattr(sample_data, "load_settings", lambda: settings)
    assert await _main() == 1
    err = capsys.readouterr().err
    assert "check DATABASE_URL" in err
    assert "wrong" not in err


@pytest.mark.parametrize(
    "host",
    ["db.abcdefghijklmnop.supabase.co", "aws-0-eu-central-1.pooler.supabase.com", "10.0.0.5"],
)
async def test_the_command_refuses_a_database_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """APP_ENV=local with a pasted cloud URL: refused before connecting."""
    url = f"postgresql+asyncpg://gize_app:secret@{host}:5432/postgres?ssl=verify-full"
    settings = make_settings(app_env=Environment.LOCAL, database_url=url)
    monkeypatch.setattr(sample_data, "load_settings", lambda: settings)
    connected: list[object] = []
    monkeypatch.setattr(sample_data, "create_engine", connected.append)
    assert await _main() == 2
    assert "a database on this machine" in capsys.readouterr().err
    assert connected == []


async def test_a_refused_step_says_how_to_recover(
    local_settings: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two runs at once both pass the emptiness check; the second's first area is taken."""

    async def taken(sessions: object) -> None:
        raise AppError(ErrorCode.AREA_NAME_TAKEN)

    monkeypatch.setattr(sample_data, "load_sample_data", taken)
    assert await _main() == 1
    err = capsys.readouterr().err
    assert "AREA_NAME_TAKEN" in err
    assert "rebuild the local database" in err
