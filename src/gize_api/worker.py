"""The background worker (Arq): `python -m gize_api.worker`, the image's `worker` command.

Not Arq's own CLI: it would replace our logging setup with plain-text logs that print job
arguments.

- **Jobs** are decorated with `@job` (one log line per outcome, a duration metric, failures
  as exception type and frames only) and listed in JOBS. They take IDs, return nothing, and
  are safe to run twice (engineering-standards.md, Reliability).
- **Retries:** a job that hits a transient failure raises `arq.Retry(defer=...)`; at most
  MAX_TRIES runs. Any other exception is a failure, logged and not retried.
- **Shutdown:** on SIGTERM the worker stops taking jobs, gives running ones
  SHUTDOWN_WAIT_SECONDS (inside the 30 s grace period) and re-queues any still running.
- **Database:** the worker connects as gize_app, like the API, with the same session rule.
"""

import functools
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

import structlog
from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import run_worker

from gize_api.core.config import ConfigError, Settings, load_settings
from gize_api.core.db import create_engine, create_sessionmaker
from gize_api.core.idempotency import purge_expired
from gize_api.core.logging import configure_logging
from gize_api.core.sentry import configure_sentry
from gize_api.core.telemetry import configure_telemetry

JOB_TIMEOUT_SECONDS = 60
MAX_TRIES = 3
MAX_JOBS = 10
SHUTDOWN_WAIT_SECONDS = 25
RESULT_KEEP_SECONDS = 24 * 3600  # a job ID is refused again for this long (de-duplication)
REDIS_TIMEOUT_SECONDS = 1
REDIS_CONNECT_RETRIES = 5

if TYPE_CHECKING:
    from arq.typing import WorkerCoroutine

JobFunction = Callable[..., Awaitable[Any]]

logger = structlog.get_logger("gize_api.worker")


def job(function: JobFunction) -> JobFunction:
    """Log and measure a job; failures as exception type and frames, never message text."""
    name = function.__name__

    @functools.wraps(function)
    async def run(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        started = time.perf_counter()
        outcome = "succeeded"
        try:
            return await function(ctx, *args, **kwargs)
        except Retry:
            outcome = "retrying"
            raise
        except BaseException as exc:
            outcome = "cancelled" if not isinstance(exc, Exception) else "failed"
            if outcome == "failed":
                logger.error("job_failed", job=name, job_try=ctx.get("job_try"), exc_info=exc)
            raise
        finally:
            duration = time.perf_counter() - started
            if outcome != "failed":
                logger.info(
                    "job_finished",
                    outcome=outcome,
                    job=name,
                    job_try=ctx.get("job_try"),
                    duration_ms=round(duration * 1000, 1),
                )
            telemetry = ctx.get("telemetry")
            if telemetry is not None:
                telemetry.record_job(name, outcome, duration)

    return run


@job
async def purge_idempotency_keys(ctx: dict[str, Any]) -> int:
    """Hourly: delete idempotency keys older than 24 h."""
    return await purge_expired(ctx["sessionmaker"])


JOBS: list[JobFunction] = []  # jobs enqueued by the API; SMS sending arrives in M2


def worker_settings(settings: Settings) -> dict[str, Any]:
    return {
        "functions": list(JOBS),
        "cron_jobs": [
            cron(
                cast("WorkerCoroutine", purge_idempotency_keys),
                minute=7,
                timeout=JOB_TIMEOUT_SECONDS,
                max_tries=1,
            )
        ],
        "redis_settings": redis_settings(settings),
        "ctx": {"settings": settings},
        "on_startup": startup,
        "on_shutdown": shutdown,
        "job_timeout": JOB_TIMEOUT_SECONDS,
        "max_tries": MAX_TRIES,
        "max_jobs": MAX_JOBS,
        "keep_result": RESULT_KEEP_SECONDS,
        "job_completion_wait": SHUTDOWN_WAIT_SECONDS,
        "log_results": False,
    }


def redis_settings(settings: Settings) -> RedisSettings:
    redis = RedisSettings.from_dsn(settings.redis_url.get_secret_value())
    redis.conn_timeout = REDIS_TIMEOUT_SECONDS
    redis.conn_retries = REDIS_CONNECT_RETRIES
    redis.ssl_check_hostname = redis.ssl  # rediss: verify the server's name too
    return redis


async def startup(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx["settings"]
    configure_logging(settings.log_level)
    logging.getLogger("arq").setLevel(logging.WARNING)  # our `@job` lines replace its own
    configure_sentry(settings)
    ctx["telemetry"] = configure_telemetry(settings)
    ctx["engine"] = create_engine(settings)
    ctx["sessionmaker"] = create_sessionmaker(ctx["engine"])
    logger.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    try:
        await ctx["engine"].dispose()
    finally:
        telemetry = ctx.get("telemetry")
        if telemetry is not None:
            await telemetry.shutdown()
        logger.info("worker_stopped")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"worker: {exc}", file=sys.stderr)  # noqa: T201 - before logging exists
        return 2
    configure_logging(settings.log_level)
    run_worker(worker_settings(settings))  # Arq accepts a dict of Worker options
    return 0


if __name__ == "__main__":  # pragma: no cover - the container's worker command
    sys.exit(main())
