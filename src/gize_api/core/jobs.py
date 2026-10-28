"""Enqueueing background jobs (run by gize_api.worker).

- Enqueue **after** the transaction that justifies the job has committed: a job enqueued
  inside one could run before the data exists, or for data that is then rolled back.
  Enqueueing with a transaction open raises OpenTransactionError.
- Every job has a `job_id` naming what it does to what (e.g. `sms:otp:<verification id>`):
  enqueueing the same ID again is refused while the job is queued or its result is kept
  (24 h), so an SMS is never sent twice for one event.
- Arguments are IDs, never personal data: jobs load what they need, and queued jobs sit in
  Redis.
"""

from typing import Any

import structlog
from arq.connections import ArqRedis
from redis.exceptions import RedisError

from gize_api.core.db import OpenTransactionError, database_transaction_open
from gize_api.core.errors import AppError, ErrorCode

QUEUE_RETRY_AFTER_SECONDS = "5"

logger = structlog.get_logger("gize_api.jobs")


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
