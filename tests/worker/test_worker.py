"""GZ-008: the Arq worker: runs jobs, retries what asks to be retried, shuts down cleanly.

Against real Redis (a queue of its own per test) and the real test database.
"""

import asyncio
import json
import signal
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import Retry
from arq.connections import ArqRedis, RedisSettings
from arq.worker import Worker
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.jobs import enqueue
from gize_api.worker import (
    SHUTDOWN_WAIT_SECONDS,
    job,
    purge_idempotency_keys,
    worker_settings,
)
from tests.support import DatabaseUrls, SettingsFactory

calls: list[str] = []


@job
async def record_call(ctx: dict[str, Any], label: str) -> None:
    calls.append(label)


@job
async def flaky(ctx: dict[str, Any], label: str) -> None:
    calls.append(label)
    if ctx["job_try"] == 1:
        raise Retry(defer=0.01)  # a transient failure: the job asks to be retried


@job
async def broken(ctx: dict[str, Any]) -> None:
    raise ValueError("a bug, with 0911223344 in the message")


@job
async def slow(ctx: dict[str, Any], seconds: float) -> None:
    await asyncio.sleep(seconds)
    calls.append(f"slow {seconds}")


@pytest.fixture
async def queue(test_redis_url: str) -> AsyncIterator[str]:
    name = f"gize-test-{uuid.uuid4()}"
    calls.clear()
    yield name
    pool = ArqRedis.from_url(test_redis_url)
    try:
        keys = [key async for key in pool.scan_iter("arq:*")]
        if keys:
            await pool.delete(*keys)
        await pool.delete(name)
    finally:
        await pool.aclose()


def _worker(
    make_settings: SettingsFactory,
    test_redis_url: str,
    database: DatabaseUrls,
    queue: str,
    **overrides: Any,  # noqa: ANN401 - Worker options
) -> Worker:
    settings = worker_settings(make_settings(database_url=database.app, redis_url=test_redis_url))
    settings.update(
        {
            "functions": [record_call, flaky, broken, slow],
            "queue_name": queue,
            "burst": True,
            "handle_signals": False,
            "poll_delay": 0.05,
            **overrides,
        }
    )
    return Worker(**settings)


async def _pool(test_redis_url: str, queue: str) -> ArqRedis:
    pool: ArqRedis = ArqRedis.from_url(test_redis_url)
    pool.default_queue_name = queue
    return pool


async def test_an_enqueued_job_runs(
    make_settings: SettingsFactory, test_redis_url: str, database: DatabaseUrls, queue: str
) -> None:
    pool = await _pool(test_redis_url, queue)
    try:
        assert await enqueue(pool, "record_call", "hello", job_id="call-1")
    finally:
        await pool.aclose()
    worker = _worker(make_settings, test_redis_url, database, queue)
    try:
        await worker.async_run()
    finally:
        await worker.close()
    assert calls == ["hello"]
    assert worker.jobs_complete == 1


async def test_the_same_job_id_is_enqueued_once(test_redis_url: str, queue: str) -> None:
    """SMS jobs are de-duplicated by their key (engineering-standards.md, Reliability)."""
    pool = await _pool(test_redis_url, queue)
    try:
        assert await enqueue(pool, "record_call", "a", job_id="sms:rider-1:otp")
        assert not await enqueue(pool, "record_call", "a", job_id="sms:rider-1:otp")
    finally:
        await pool.aclose()


async def test_a_job_that_asks_for_a_retry_runs_again(
    make_settings: SettingsFactory, test_redis_url: str, database: DatabaseUrls, queue: str
) -> None:
    pool = await _pool(test_redis_url, queue)
    try:
        await enqueue(pool, "flaky", "f", job_id="flaky-1")
    finally:
        await pool.aclose()
    worker = _worker(make_settings, test_redis_url, database, queue)
    try:
        await worker.async_run()
    finally:
        await worker.close()
    assert calls == ["f", "f"]
    assert worker.jobs_complete == 1


async def test_a_failing_job_is_logged_without_its_message(
    make_settings: SettingsFactory,
    test_redis_url: str,
    database: DatabaseUrls,
    queue: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pool = await _pool(test_redis_url, queue)
    try:
        await enqueue(pool, "broken", job_id="broken-1")
    finally:
        await pool.aclose()
    worker = _worker(make_settings, test_redis_url, database, queue)
    try:
        await worker.async_run()
    finally:
        await worker.close()
    assert worker.jobs_failed == 1
    output = capsys.readouterr().err
    assert "0911223344" not in output
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    (failed,) = [line for line in lines if line["event"] == "job_failed"]
    assert failed["job"] == "broken"
    assert failed["exception_type"] == "ValueError"


async def test_shutdown_lets_a_short_job_finish(
    make_settings: SettingsFactory, test_redis_url: str, database: DatabaseUrls, queue: str
) -> None:
    pool = await _pool(test_redis_url, queue)
    try:
        await enqueue(pool, "slow", 0.3, job_id="slow-short")
    finally:
        await pool.aclose()
    worker = _worker(
        make_settings, test_redis_url, database, queue, burst=False, job_completion_wait=2
    )
    run = asyncio.create_task(worker.async_run())
    await asyncio.sleep(0.15)  # the job is running
    worker.handle_sig_wait_for_completion(signal.SIGTERM)
    with pytest.raises(asyncio.CancelledError):
        await run
    await worker.close()
    assert calls == ["slow 0.3"]


async def test_shutdown_requeues_a_job_that_cannot_finish_in_time(
    make_settings: SettingsFactory, test_redis_url: str, database: DatabaseUrls, queue: str
) -> None:
    pool = await _pool(test_redis_url, queue)
    try:
        await enqueue(pool, "slow", 5.0, job_id="slow-long")
        worker = _worker(
            make_settings, test_redis_url, database, queue, burst=False, job_completion_wait=0.2
        )
        run = asyncio.create_task(worker.async_run())
        await asyncio.sleep(0.15)
        worker.handle_sig_wait_for_completion(signal.SIGTERM)
        with pytest.raises(asyncio.CancelledError):
            await run
        # The cancelled job re-queues itself as it ends; then the worker can close.
        await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        worker.tasks.clear()
        await worker.close()
        assert calls == []
        assert await pool.zcard(queue) == 1  # back in the queue for the next worker
    finally:
        await pool.aclose()


def test_worker_settings(make_settings: SettingsFactory) -> None:
    settings = worker_settings(make_settings(redis_url="rediss://:secret@cache.example.com:6380/2"))
    redis = settings["redis_settings"]
    assert isinstance(redis, RedisSettings)
    assert (redis.host, redis.port, redis.database, redis.ssl) == (
        "cache.example.com",
        6380,
        2,
        True,
    )
    assert redis.conn_timeout == 1
    assert settings["job_completion_wait"] == SHUTDOWN_WAIT_SECONDS < 30  # inside the grace period
    assert settings["max_tries"] == 3
    assert settings["job_timeout"] == 60
    assert [cron.name for cron in settings["cron_jobs"]] == ["cron:purge_idempotency_keys"]


async def test_the_purge_job_uses_the_workers_database(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    from gize_api.worker import shutdown, startup

    ctx: dict[str, Any] = {"settings": make_settings(database_url=database.app)}
    await startup(ctx)
    try:
        assert await purge_idempotency_keys(ctx) == 0
    finally:
        await shutdown(ctx)


async def test_enqueue_refuses_inside_a_database_transaction(
    db_session: AsyncSession, test_redis_url: str
) -> None:
    from gize_api.core.http import OpenTransactionError

    pool = ArqRedis.from_url(test_redis_url)
    try:
        async with db_session.begin():
            with pytest.raises(OpenTransactionError):
                await enqueue(pool, "record_call", "x", job_id="in-transaction")
    finally:
        await pool.aclose()


async def test_enqueue_with_redis_down_is_503() -> None:
    pool = ArqRedis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        with pytest.raises(AppError) as exc_info:
            await enqueue(pool, "record_call", "x", job_id="redis-down")
    finally:
        await pool.aclose()
    assert exc_info.value.code is ErrorCode.SERVICE_UNAVAILABLE


def test_main_refuses_to_start_without_settings(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gize_api.worker import main

    assert main() == 2
    assert "worker: invalid configuration" in capsys.readouterr().err


def test_main_runs_the_worker_with_our_settings(
    clean_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, test_redis_url: str
) -> None:
    from gize_api import worker as module

    clean_env.setenv("APP_ENV", "test")
    clean_env.setenv("REDIS_URL", test_redis_url)
    clean_env.setenv("DATABASE_URL", "postgresql://gize_app:pw@127.0.0.1:1/gize")
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "run_worker", started.append)  # not Arq's CLI or its logging
    assert module.main() == 0
    (settings,) = started
    assert settings["on_startup"] is module.startup


async def test_jobs_are_measured_and_telemetry_is_shut_down(
    make_settings: SettingsFactory, database: DatabaseUrls
) -> None:
    from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

    from gize_api.core.db import create_engine
    from gize_api.core.telemetry import configure_telemetry
    from gize_api.worker import shutdown

    metrics = InMemoryMetricReader()
    telemetry = configure_telemetry(make_settings(), metric_reader=metrics)
    ctx: dict[str, Any] = {"telemetry": telemetry, "job_try": 1}
    calls.clear()
    await record_call(ctx, "measured")
    data = metrics.get_metrics_data()
    assert data is not None
    points = [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "gize.job.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    ]
    assert points == [{"job": "record_call", "outcome": "succeeded"}]
    ctx["engine"] = create_engine(make_settings(database_url=database.app))
    await shutdown(ctx)
