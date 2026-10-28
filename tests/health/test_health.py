"""GZ-007: liveness and readiness, as the load balancer sees them.

Readiness depends on Postgres only; Redis is checked, logged and counted, never decisive
(engineering-standards.md, "Health").
"""

import asyncio
import json
import time

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from gize_api.core.telemetry import configure_telemetry
from gize_api.health.readiness import READINESS_TIMEOUT_SECONDS, Readiness
from gize_api.main import GizeAPI, create_app
from tests.support import UNREACHABLE_DATABASE_URL, DatabaseUrls, SettingsFactory

pytestmark = pytest.mark.asyncio

OK = {"status": "ok"}


def _client(app: GizeAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    return [line for line in lines if isinstance(line, dict)]


@pytest.fixture
def blackhole_host_port(blackhole_url: str) -> str:
    return blackhole_url.removeprefix("http://")


async def _get(app: GizeAPI, path: str, method: str = "GET") -> httpx.Response:
    async with app.router.lifespan_context(app), _client(app) as client:
        return await client.request(method, path)


async def test_ready_when_postgres_and_redis_answer(
    make_settings: SettingsFactory, database: DatabaseUrls, test_redis_url: str
) -> None:
    app = create_app(make_settings(database_url=database.app, redis_url=test_redis_url))
    response = await _get(app, "/health/ready")
    assert response.status_code == 200
    assert response.json() == OK
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_head_is_answered(
    make_settings: SettingsFactory, database: DatabaseUrls, test_redis_url: str, path: str
) -> None:
    app = create_app(make_settings(database_url=database.app, redis_url=test_redis_url))
    assert (await _get(app, path, method="HEAD")).status_code == 200


async def test_live_answers_while_every_dependency_is_down(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())  # unreachable Postgres and Redis
    response = await _get(app, "/health/live")
    assert response.status_code == 200
    assert response.json() == OK
    assert response.headers["Cache-Control"] == "no-store"


async def test_not_ready_without_postgres(
    make_settings: SettingsFactory,
    test_redis_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(make_settings(redis_url=test_redis_url))  # unreachable Postgres
    response = await _get(app, "/health/ready")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert set(body) == {"type", "title", "status", "code", "request_id"}
    for internal in ("postgres", "redis", "database", "127.0.0.1", "asyncpg"):
        assert internal not in response.text.lower()
    (line,) = [entry for entry in _log_lines(capsys) if entry["event"] == "dependency_unavailable"]
    assert line["dependency"] == "postgres"
    assert line["level"] == "error"
    assert line["exception_type"]


async def test_redis_down_is_reported_but_not_decisive(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shared Redis outage must not take every instance out of rotation."""
    metrics = InMemoryMetricReader()
    app = create_app(make_settings(database_url=database.app))  # unreachable Redis
    app.telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    response = await _get(app, "/health/ready")
    assert response.status_code == 200
    assert response.json() == OK
    (line,) = [entry for entry in _log_lines(capsys) if entry["event"] == "dependency_unavailable"]
    assert line["dependency"] == "redis"
    assert line["level"] == "error"
    data = metrics.get_metrics_data()
    assert data is not None
    points = [
        (dict(point.attributes or {}), point.value)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "gize.dependency.unavailable"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    ]
    assert [(p["dependency"], value) for p, value in points] == [("redis", 1)]


@pytest.mark.parametrize("hung", ["postgres", "redis"])
async def test_a_hung_dependency_is_bounded(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    test_redis_url: str,
    blackhole_host_port: str,
    hung: str,
) -> None:
    """A server that accepts connections and never answers costs at most the timeout."""
    settings = (
        make_settings(
            database_url=f"postgresql://gize_app:pw@{blackhole_host_port}/gize",
            redis_url=test_redis_url,
        )
        if hung == "postgres"
        else make_settings(database_url=database.app, redis_url=f"redis://{blackhole_host_port}/0")
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app), _client(app) as client:
        started = time.monotonic()
        response = await client.get("/health/ready")
        elapsed = time.monotonic() - started
    assert response.status_code == (503 if hung == "postgres" else 200)
    assert elapsed < READINESS_TIMEOUT_SECONDS + 0.5


async def test_a_burst_of_probes_checks_once(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    test_redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is public: a flood of requests must not become a flood of connections."""
    calls: list[str] = []
    original = Readiness._check_postgres

    async def counting(self: Readiness) -> bool:
        calls.append("postgres")
        return await original(self)

    monkeypatch.setattr(Readiness, "_check_postgres", counting)
    monkeypatch.setattr("gize_api.health.readiness.READINESS_CACHE_SECONDS", 0.3)
    app = create_app(make_settings(database_url=database.app, redis_url=test_redis_url))
    async with app.router.lifespan_context(app), _client(app) as client:
        responses = await asyncio.gather(*(client.get("/health/ready") for _ in range(20)))
        assert {response.status_code for response in responses} == {200}
        assert (await client.get("/health/ready")).status_code == 200  # still fresh
        assert calls == ["postgres"]
        await asyncio.sleep(0.35)
        assert (await client.get("/health/ready")).status_code == 200
    assert calls == ["postgres", "postgres"]


async def test_successful_probes_stay_out_of_the_access_log(
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    test_redis_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(make_settings(database_url=database.app, redis_url=test_redis_url))
    async with app.router.lifespan_context(app), _client(app) as client:
        await client.get("/health/live")
        await client.get("/health/ready")
        await client.get("/no-such-route")
    routes = [entry.get("route") for entry in _log_lines(capsys) if entry["event"] == "request"]
    assert routes == ["unmatched"]


async def test_a_failed_probe_is_in_the_access_log(
    make_settings: SettingsFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(make_settings(database_url=UNREACHABLE_DATABASE_URL))
    await _get(app, "/health/ready")
    lines = [entry for entry in _log_lines(capsys) if entry["event"] == "request"]
    assert [(line["route"], line["status"]) for line in lines] == [("/health/ready", 503)]


async def test_health_is_not_in_the_api_schema(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings(app_env="local"))
    assert not [path for path in app.openapi()["paths"] if path.startswith("/health")]


async def test_only_get_and_head(make_settings: SettingsFactory) -> None:
    app = create_app(make_settings())
    for path in ("/health/live", "/health/ready"):
        assert (await _get(app, path, method="POST")).status_code == 405
