"""GZ-006: engine settings, the session dependency, and database failures as 503."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import QueuePool

from gize_api.core.db import (
    CONNECT_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_MS,
    create_engine,
    get_session,
)
from gize_api.main import create_app
from tests.support import DatabaseUrls, SettingsFactory


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _app(make_settings: SettingsFactory, database_url: str) -> FastAPI:
    app = create_app(make_settings(database_url=database_url))

    @app.get("/_test/now")
    async def now(session: AsyncSession = Depends(get_session)) -> dict[str, str]:  # noqa: B008
        value = await session.scalar(select(func.now()))
        return {"now": str(value)}

    @app.get("/_test/slow")
    async def slow(session: AsyncSession = Depends(get_session)) -> None:  # noqa: B008
        await session.execute(select(func.pg_sleep(30)))

    return app


async def test_session_dependency_serves_requests(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    app = _app(make_settings, database.app)
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.get("/_test/now")
    assert response.status_code == 200


async def test_engine_settings(make_settings: SettingsFactory) -> None:
    engine = create_engine(make_settings())
    try:
        assert engine.sync_engine.hide_parameters is True
        pool = engine.sync_engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.timeout() == 3
        assert CONNECT_TIMEOUT_SECONDS == 3
        assert STATEMENT_TIMEOUT_MS == 5000
    finally:
        await engine.dispose()


async def test_statement_timeout_applies_to_app_connections(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    engine = create_engine(make_settings(database_url=database.app))
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SHOW statement_timeout")) == "5s"
    finally:
        await engine.dispose()


async def test_database_unreachable_is_503(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    app = _app(make_settings, "postgresql://gize_app:pw@127.0.0.1:1/gize")
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.get("/_test/now")
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    (line,) = [entry for entry in lines if entry["event"] == "database_unavailable"]
    assert line["exception_type"]
    assert "127.0.0.1" not in json.dumps(line)


async def test_database_that_never_answers_is_503_within_the_connect_timeout(
    make_settings: SettingsFactory, blackhole_url: str
) -> None:
    host_port = blackhole_url.removeprefix("http://")
    app = _app(make_settings, f"postgresql://gize_app:pw@{host_port}/gize")
    started = time.monotonic()
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.get("/_test/now")
    assert response.status_code == 503
    assert time.monotonic() - started < CONNECT_TIMEOUT_SECONDS + 2


async def test_statement_timeout_is_503(
    make_settings: SettingsFactory, database: DatabaseUrls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gize_api.core.db.STATEMENT_TIMEOUT_MS", 200)
    app = _app(make_settings, database.app)
    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.get("/_test/slow")
    assert response.status_code == 503
    body: dict[str, Any] = response.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert "pg_sleep" not in response.text


async def test_lifespan_disposes_the_engine(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    app = _app(make_settings, database.app)
    async with app.router.lifespan_context(app):
        engine = app.state.engine
        async with app.state.sessionmaker() as session:
            await session.scalar(select(func.now()))
        assert engine.pool.checkedin() == 1
    assert not hasattr(app.state, "engine")
    assert engine.pool.checkedin() == 0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h/d?ssl=require", "require"),
        ("postgresql://u:p@h/d", None),
    ],
)
def test_ssl_connect_argument(url: str, expected: str | None) -> None:
    from gize_api.core.db import ssl_connect_argument

    assert ssl_connect_argument(url).get("ssl") == expected


def test_verify_full_uses_a_verifying_context() -> None:
    import ssl

    from gize_api.core.db import ssl_connect_argument

    context = ssl_connect_argument("postgresql://u:p@h/d?ssl=verify-full")["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


async def test_classifying_database_errors(database: DatabaseUrls) -> None:
    import sqlalchemy.exc

    from gize_api.core.db import is_database_unavailable

    assert is_database_unavailable(sqlalchemy.exc.TimeoutError())
    assert not is_database_unavailable(OSError("not from the driver"))
    engine = create_engine_for(database.app)
    try:
        async with engine.connect() as conn:
            with pytest.raises(sqlalchemy.exc.DBAPIError) as syntax_error:
                await conn.execute(text("SELEC 1"))
        assert not is_database_unavailable(syntax_error.value)
        async with engine.connect() as conn:
            await conn.execute(text("SET statement_timeout = 50"))
            with pytest.raises(sqlalchemy.exc.DBAPIError) as cancelled:
                await conn.execute(text("SELECT pg_sleep(1)"))
        assert is_database_unavailable(cancelled.value)
    finally:
        await engine.dispose()


def create_engine_for(url: str):  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from gize_api.core.db import async_url

    return create_async_engine(async_url(url), poolclass=NullPool)


async def test_other_database_errors_stay_500(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    app = create_app(make_settings(database_url=database.app))

    @app.get("/_test/bad-sql")
    async def bad_sql(session: AsyncSession = Depends(get_session)) -> None:  # noqa: B008
        await session.execute(text("SELECT * FROM gize.no_such_table"))

    @app.get("/_test/other-oserror")
    async def other_oserror() -> None:
        raise FileNotFoundError("not a database problem")

    async with app.router.lifespan_context(app), _client(app) as client:
        for path in ("/_test/bad-sql", "/_test/other-oserror"):
            response = await client.get(path)
            assert response.status_code == 500, path
            assert response.json()["code"] == "INTERNAL_ERROR"


async def test_engine_dispose_failure_does_not_stop_shutdown(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    async def failing_dispose(self: AsyncEngine, close: bool = True) -> None:
        raise RuntimeError("dispose failed")

    monkeypatch.setattr(AsyncEngine, "dispose", failing_dispose)
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        pass
    assert '"database_dispose_failed"' in capsys.readouterr().err


async def test_a_killed_connection_is_unavailable(database: DatabaseUrls) -> None:
    import asyncpg
    import sqlalchemy.exc

    from gize_api.core.db import is_database_unavailable

    engine = create_engine_for(database.app)
    admin = await asyncpg.connect(database.admin, timeout=3)
    try:
        async with engine.connect() as conn:
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            await admin.execute("SELECT pg_terminate_backend($1)", pid)
            with pytest.raises(sqlalchemy.exc.DBAPIError) as lost:
                await conn.execute(text("SELECT 1"))
        assert is_database_unavailable(lost.value)
    finally:
        await admin.close()
        await engine.dispose()


@pytest.mark.parametrize(
    ("sqlstate", "unavailable"),
    [
        ("40P01", True),  # deadlock: retry later
        ("40001", True),  # serialization failure: retry later
        ("53200", True),  # out of memory
        ("53300", True),  # too many connections
        ("53100", True),  # disk full
        ("XX000", False),  # an internal error from a statement is a bug report
        ("23505", False),  # unique violation: the caller's problem, never a 503
    ],
)
async def test_classifying_errors_by_sqlstate(
    database: DatabaseUrls, sqlstate: str, unavailable: bool
) -> None:
    import sqlalchemy.exc

    from gize_api.core.db import is_database_unavailable

    engine = create_engine_for(database.app)
    try:
        async with engine.connect() as conn:
            with pytest.raises(sqlalchemy.exc.DBAPIError) as raised:
                await conn.execute(
                    text(f"DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '{sqlstate}'; END $$")
                )
        assert is_database_unavailable(raised.value) is unavailable
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "broken",
    [
        lambda url: url.replace("gize-app-test", "wrong-password"),  # e.g. a rotated password
        lambda url: url.rsplit("/", 1)[0] + "/no_such_database",
    ],
)
async def test_errors_while_connecting_are_unavailable(
    database: DatabaseUrls, broken: Callable[[str], str]
) -> None:
    """Whatever the server says while a connection is being opened (a pooler at its client
    limit included), no statement ran: the database is unavailable, not the code wrong."""
    import sqlalchemy.exc

    from gize_api.core.db import is_database_unavailable

    engine = create_engine_for(broken(database.app))
    try:
        with pytest.raises(sqlalchemy.exc.DBAPIError) as raised:
            async with engine.connect():
                pass
        assert is_database_unavailable(raised.value)
    finally:
        await engine.dispose()


async def test_engine_pool_follows_settings(make_settings: SettingsFactory) -> None:
    engine = create_engine(make_settings(database_pool_size=2, database_max_overflow=1))
    try:
        pool = engine.sync_engine.pool
        assert isinstance(pool, QueuePool)
        assert (pool.size(), pool._max_overflow) == (2, 1)
    finally:
        await engine.dispose()


def test_verify_full_engine_uses_the_root_certificate(make_settings: SettingsFactory) -> None:
    import ssl

    import certifi

    from gize_api.core.db import _connect_args

    settings = make_settings(
        database_url="postgresql://gize_app:pw@db.example.com/postgres?ssl=verify-full",
        database_ssl_root_cert=certifi.where(),
    )
    context = _connect_args(settings)["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.get_ca_certs()


async def test_db_session_uses_the_apps_connection_settings(db_session: AsyncSession) -> None:
    assert await db_session.scalar(text("SHOW application_name")) == "gize-api"
    assert await db_session.scalar(text("SELECT current_user")) == "gize_app"


async def test_app_with_db_session_serves_requests_from_the_test_transaction(
    app_with_db_session: FastAPI, db_session: AsyncSession
) -> None:
    @app_with_db_session.get("/_test/marker")
    async def marker(session: AsyncSession = Depends(get_session)) -> dict[str, str]:  # noqa: B008
        return {"value": str(await session.scalar(text("SELECT current_setting('gize.marker')")))}

    await db_session.execute(text("SELECT set_config('gize.marker', 'from-the-test', true)"))
    async with _client(app_with_db_session) as client:
        response = await client.get("/_test/marker")
    assert response.json() == {"value": "from-the-test"}


async def test_unavailable_database_is_logged_with_its_sqlstate(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(make_settings(database_url=database.app))

    @app.get("/_test/deadlock")
    async def deadlock(session: AsyncSession = Depends(get_session)) -> None:  # noqa: B008
        await session.execute(text("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '40P01'; END $$"))

    async with app.router.lifespan_context(app), _client(app) as client:
        response = await client.get("/_test/deadlock")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    (line,) = [entry for entry in lines if entry["event"] == "database_unavailable"]
    assert line["sqlstate"] == "40P01"


@pytest.mark.parametrize("error", [FileNotFoundError("app"), TimeoutError("app")])
async def test_app_errors_inside_the_orm_are_not_unavailable(error: Exception) -> None:
    """ORM callbacks run inside SQLAlchemy's greenlet: their own errors are bugs (500)."""
    from sqlalchemy.util import greenlet_spawn

    from gize_api.core.db import is_database_unavailable

    def app_code() -> None:
        raise error

    with pytest.raises(type(error)) as raised:
        await greenlet_spawn(app_code)
    assert not is_database_unavailable(raised.value)


async def test_a_hung_server_timeout_is_unavailable(database: DatabaseUrls) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from gize_api.core.db import async_url, is_database_unavailable

    engine = create_async_engine(
        async_url(database.app), poolclass=NullPool, connect_args={"command_timeout": 0.2}
    )
    try:
        async with engine.connect() as conn:
            with pytest.raises(TimeoutError) as raised:
                await conn.execute(text("SELECT pg_sleep(2)"))
        assert is_database_unavailable(raised.value)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(("broken", "reported"), [("password", True), ("statement", False)])
async def test_misconfiguration_is_reported_to_sentry_but_outages_are_not(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
    reported: bool,
) -> None:
    """A wrong password or database name will not fix itself: someone must be told."""
    reports: list[str] = []
    monkeypatch.setattr(
        "gize_api.core.errors.report_exception",
        lambda exc, **_: reports.append(type(exc).__name__),
    )
    url = database.app.replace("gize-app-test", "wrong") if broken == "password" else database.app
    app = create_app(make_settings(database_url=url))

    @app.get("/_test/db")
    async def db(session: AsyncSession = Depends(get_session)) -> None:  # noqa: B008
        await session.execute(text("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '57014'; END $$"))

    async with app.router.lifespan_context(app), _client(app) as client:
        assert (await client.get("/_test/db")).status_code == 503
    assert bool(reports) is reported
