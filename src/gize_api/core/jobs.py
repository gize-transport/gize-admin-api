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
import uuid
from typing import Any

import structlog
from arq.connections import ArqRedis
from redis.asyncio import ConnectionPool
from redis.exceptions import RedisError

from gize_api.core.db import OpenTransactionError, database_transaction_open
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.redis_options import connection_options

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
    return ArqRedis(pool, job_serializer=serialize_job, job_deserializer=deserialize_job)


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


async def enqueue(pool: ArqRedis, function: str, *args: Any, job_id: str) -> bool:  # noqa: ANN401
    """Queue `function(*args)`; False if a job with this ID is already queued or done."""
    if database_transaction_open():
        raise OpenTransactionError("enqueue jobs after the transaction has committed")
    try:
        queued = await pool.enqueue_job(function, *args, _job_id=job_id)
    except (RedisError, OSError) as exc:
        logger.error("job_queue_unavailable", job=function, exc_info=exc)
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE, headers={"Retry-After": QUEUE_RETRY_AFTER_SECONDS}
        ) from None
    if queued is None:
        logger.info("job_already_enqueued", job=function)
        return False
    return True
