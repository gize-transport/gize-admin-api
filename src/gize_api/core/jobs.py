"""Enqueueing background jobs (run by gize_api.worker).

- Enqueue **after** the transaction that justifies the job has committed: a job enqueued
  inside one could run before the data exists, or for data that is then rolled back.
  Enqueueing with a transaction open raises OpenTransactionError.
- Every job has a `job_id` naming what it does to what (e.g. `sms:otp:<verification id>`):
  enqueueing the same ID again is refused while the job is queued or its result is kept
  (24 h), so an SMS is never sent twice for one event.
- Arguments are IDs, never personal data: jobs load what they need, and queued jobs sit in
  Redis.
- Jobs travel as JSON, never pickle: with pickle, anyone able to write to Redis could run
  code in the worker. A failed job's stored result holds only its exception type.
"""

import json
import time
import uuid
from typing import Any

import structlog
from arq.connections import ArqRedis
from redis.asyncio import ConnectionPool
from redis.exceptions import RedisError

from gize_api.core.db import OpenTransactionError, database_transaction_open
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.redis_options import connection_options
from gize_api.core.telemetry import Telemetry

QUEUE_RETRY_AFTER_SECONDS = "5"
REDIS_TIMEOUT_SECONDS = 1.0

logger = structlog.get_logger("gize_api.jobs")


def create_job_redis(
    url: str,
    *,
    timeout: float = REDIS_TIMEOUT_SECONDS,
    max_connections: int = 50,
    client_name: str = "gize-api",
) -> ArqRedis:
    """A Redis client that can also enqueue jobs, with JSON job serialisation."""
    pool = ConnectionPool.from_url(
        url,
        **connection_options(
            url, timeout=timeout, max_connections=max_connections, client_name=client_name
        ),
    )
    client = ArqRedis(pool, job_serializer=serialize_job, job_deserializer=deserialize_job)
    # redis-py leaves a pool passed in from outside open on aclose(); this client owns it.
    client.auto_close_connection_pool = True
    return client


class JobFailedError(RuntimeError):
    """Stands in for a failed job's exception in Arq's stored result.

    It carries only the original exception's type name, never its message (which can hold
    data); results serve de-duplication, not reading back.
    """


def serialize_job(data: dict[str, Any]) -> bytes:
    return json.dumps(data, default=_encode, separators=(",", ":")).encode()


def deserialize_job(raw: bytes) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(raw)
    return data


def _encode(value: object) -> object:
    if isinstance(value, JobFailedError):  # a failed job's result: the original type only
        return {"error": str(value)}
    if isinstance(value, BaseException):  # anything else Arq stores: its type, no message
        return {"error": type(value).__name__}
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"job data must be JSON (IDs), not {type(value).__name__}")


async def enqueue(
    pool: ArqRedis,
    function: str,
    *args: Any,  # noqa: ANN401 - job arguments (IDs)
    job_id: str,
    telemetry: Telemetry | None,
) -> bool:
    """Queue `function(*args)`; False if a job with this ID is already queued or done.

    `telemetry` is required (pass `request.app.telemetry`) so every call is measured.
    """
    if database_transaction_open():
        raise OpenTransactionError("enqueue jobs after the transaction has committed")
    started = time.perf_counter()
    try:
        queued = await pool.enqueue_job(function, *args, _job_id=job_id)
    except (RedisError, OSError) as exc:
        _measure(telemetry, "error", started)
        logger.error("job_queue_unavailable", job=function, exc_info=exc)
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE, headers={"Retry-After": QUEUE_RETRY_AFTER_SECONDS}
        ) from None
    if queued is None:
        _measure(telemetry, "duplicate", started)
        logger.info("job_already_enqueued", job=function)
        return False
    _measure(telemetry, "ok", started)
    return True


def _measure(telemetry: Telemetry | None, outcome: str, started: float) -> None:
    if telemetry is not None:
        telemetry.record_redis_operation("enqueue", outcome, time.perf_counter() - started)
