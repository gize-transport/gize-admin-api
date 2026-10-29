"""GZ-009: every Redis operation of ours is measured (moved here from GZ-005).

The Redis instrumentation records spans only, so the rate limiter, job enqueueing and the
readiness ping each record db.client.operation.duration (db.system.name=redis).
"""

import uuid

import pytest
from arq.connections import ArqRedis
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from gize_api.core.errors import AppError
from gize_api.core.jobs import create_job_redis, enqueue
from gize_api.core.telemetry import Telemetry, configure_telemetry
from gize_api.health.readiness import Readiness
from tests.support import DatabaseUrls, SettingsFactory


def _redis_points(metrics: InMemoryMetricReader) -> list[tuple[str, str]]:
    data = metrics.get_metrics_data()
    assert data is not None
    return sorted(
        (
            str((point.attributes or {})["db.operation.name"]),
            str((point.attributes or {})["outcome"]),
        )
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "db.client.operation.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
        and (point.attributes or {}).get("db.system.name") == "redis"
    )


@pytest.fixture
def telemetry(make_settings: SettingsFactory) -> tuple[Telemetry, InMemoryMetricReader]:
    metrics = InMemoryMetricReader()
    configured = configure_telemetry(make_settings(), metric_reader=metrics)
    assert configured is not None
    return configured, metrics


async def test_enqueueing_is_measured(
    telemetry: tuple[Telemetry, InMemoryMetricReader], test_redis_url: str
) -> None:
    configured, metrics = telemetry
    pool: ArqRedis = create_job_redis(test_redis_url)
    pool.default_queue_name = f"gize-test-{uuid.uuid4()}"
    down: ArqRedis = create_job_redis("redis://127.0.0.1:1/0")
    job_id = f"metric-{uuid.uuid4()}"
    try:
        await enqueue(pool, "noop", job_id=job_id, telemetry=configured)
        await enqueue(pool, "noop", job_id=job_id, telemetry=configured)  # a duplicate
        with pytest.raises(AppError):
            await enqueue(down, "noop", job_id=job_id, telemetry=configured)
        await pool.delete(f"arq:job:{job_id}", pool.default_queue_name)
    finally:
        await pool.aclose()
        await down.aclose()
    assert _redis_points(metrics) == [
        ("enqueue", "duplicate"),
        ("enqueue", "error"),
        ("enqueue", "ok"),
    ]


async def test_the_readiness_ping_is_measured(
    telemetry: tuple[Telemetry, InMemoryMetricReader],
    make_settings: SettingsFactory,
    database: DatabaseUrls,
    test_redis_url: str,
) -> None:
    configured, metrics = telemetry
    for redis_url in (test_redis_url, "redis://127.0.0.1:1/0"):
        readiness = Readiness.for_settings(
            make_settings(database_url=database.app, redis_url=redis_url), configured
        )
        try:
            await readiness._check_redis()
        finally:
            await readiness.close()
    assert _redis_points(metrics) == [("ping", "error"), ("ping", "ok")]


async def test_closing_the_job_client_closes_its_connections(test_redis_url: str) -> None:
    """redis-py leaves a pool passed in from outside open on close; ours must not."""
    from redis.asyncio import Redis

    name = f"gize-test-close-{uuid.uuid4().hex[:8]}"
    client = create_job_redis(test_redis_url, client_name=name)
    await client.ping()
    await client.aclose()
    async with Redis.from_url(test_redis_url) as observer:
        names = [entry["name"] for entry in await observer.client_list()]
        for _ in range(20):  # Redis drops a closed client on its next pass; no sleeping
            if name not in names:
                break
            names = [entry["name"] for entry in await observer.client_list()]
    assert name not in names
