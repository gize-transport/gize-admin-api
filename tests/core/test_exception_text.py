"""GZ-005 (owner decision): exception messages and source lines never reach logs or traces.

Every route by which exception text could still be written is covered here: exceptions
passed as values or format arguments, library records with tracebacks in their message,
uvicorn's lifespan failures, uncaught exceptions in threads and at process level, logging's
own error reporting, warnings, and errors escaping our middleware.
"""

import asyncio
import gc
import json
import logging
import sys
import threading
import warnings
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import sentry_sdk
import structlog
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis
from sentry_sdk.types import Event, Hint
from starlette.types import Message, Receive, Scope, Send
from uvicorn.config import Config
from uvicorn.lifespan.on import LifespanOn

from gize_api.core import request_id as request_id_module
from gize_api.core.logging import configure_logging, log_security_event
from gize_api.core.telemetry import configure_telemetry
from gize_api.main import create_app
from tests.support import SettingsFactory

SENSITIVE = "Abebe Kebede 0911223344"


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]


def _boom() -> ValueError:
    try:
        raise ValueError(f"rider {SENSITIVE}")
    except ValueError as exc:
        return exc


@pytest.mark.parametrize(
    "emit",
    [
        lambda: structlog.get_logger("t").error("x", err=_boom()),
        lambda: structlog.get_logger("t").error(_boom()),
        lambda: structlog.get_logger("t").error("failed %s", _boom()),
        lambda: logging.getLogger("lib").error("failed: %s", _boom()),
        lambda: logging.getLogger("lib").error(_boom()),
        lambda: logging.getLogger("lib").error("failed: %(e)s", {"e": _boom()}),
    ],
    ids=["field", "event", "structlog-arg", "stdlib-arg", "stdlib-msg", "stdlib-dict-arg"],
)
def test_exceptions_as_values_become_their_type(
    capsys: pytest.CaptureFixture[str], emit: Callable[[], None]
) -> None:
    configure_logging("INFO")
    emit()
    output = capsys.readouterr().err
    assert "ValueError" in output
    assert "Abebe" not in output
    assert "0911223344" not in output


def test_security_event_extras_carry_only_exception_types() -> None:
    events: list[Event] = []

    def capture(event: Event, hint: Hint) -> None:
        events.append(event)  # captured, never sent

    sentry_sdk.init(
        dsn="https://p@sentry.example.invalid/1",
        default_integrations=False,
        auto_enabling_integrations=False,
        before_send=capture,
    )
    try:
        configure_logging("INFO")
        log_security_event("sec", err=_boom())
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init(default_integrations=False, auto_enabling_integrations=False)
    assert events
    assert "Abebe" not in json.dumps(events, default=str)


def test_library_record_with_traceback_keeps_only_its_first_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    exc = _boom()
    logging.getLogger("asyncio").error(
        "Task exception was never retrieved\nfuture: <Task exception=%r>",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    (line,) = _lines(capsys)
    assert line["event"] == "Task exception was never retrieved"
    assert line["exception_type"] == "ValueError"
    assert "Abebe" not in json.dumps(line)


async def test_never_retrieved_task_exception_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    async def failing() -> None:
        raise ValueError(f"task {SENSITIVE}")

    task = asyncio.get_running_loop().create_task(failing())
    await asyncio.sleep(0)
    del task
    gc.collect()
    output = capsys.readouterr().err
    assert "Abebe" not in output
    assert "0911223344" not in output


def test_preformatted_traceback_text_is_suppressed(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    traceback_text = (
        'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    redis = make()\n'
        f"ValueError: {SENSITIVE}"
    )
    logging.getLogger("uvicorn.error").error(traceback_text)
    (line,) = _lines(capsys)
    assert line["event"] == "traceback_text_suppressed"
    assert "redis = make()" not in json.dumps(line)


async def test_lifespan_failure_is_logged_without_text(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_from_url(*args: object, **kwargs: object) -> Redis:
        raise ValueError(f"cannot connect for {SENSITIVE}")

    monkeypatch.setattr(Redis, "from_url", failing_from_url)
    app = create_app(make_settings())
    # log_config=None: uvicorn configures its logging before calling the app factory in
    # production, so ours is the configuration in force (as it is here).
    lifespan = LifespanOn(Config(app=app, lifespan="on", log_config=None))
    await lifespan.startup()
    assert lifespan.startup_failed
    output = capsys.readouterr().err
    assert "Abebe" not in output
    assert "redis = Redis.from_url" not in output  # no source lines either
    failed = [
        line for line in map(json.loads, output.splitlines()) if line["event"] == "lifespan_failed"
    ]
    assert failed
    assert failed[0]["exception_type"] == "ValueError"


def test_uncaught_thread_exception_goes_through_the_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    exc = _boom()
    threading.excepthook(
        threading.ExceptHookArgs((type(exc), exc, exc.__traceback__, threading.current_thread()))
    )
    (line,) = _lines(capsys)
    assert line["event"] == "uncaught_thread_exception"
    assert line["exception_type"] == "ValueError"
    assert "Abebe" not in json.dumps(line)


def test_uncaught_process_exception_goes_through_the_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    exc = _boom()
    sys.excepthook(type(exc), exc, exc.__traceback__)
    (line,) = _lines(capsys)
    assert line["event"] == "uncaught_exception"
    assert "Abebe" not in json.dumps(line)


def test_unraisable_exception_goes_through_the_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    exc = _boom()

    class Unraisable:
        exc_type = type(exc)
        exc_value = exc
        exc_traceback = exc.__traceback__
        err_msg = f"Exception ignored in {SENSITIVE}"
        object = None

    sys.unraisablehook(Unraisable)  # type: ignore[arg-type]
    (line,) = _lines(capsys)
    assert line["event"] == "unraisable_exception"
    assert "Abebe" not in json.dumps(line)


def test_logging_errors_never_dump_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("lib").info("rider %s phone %s", "Abebe Kebede")  # one argument missing
    err = capsys.readouterr().err
    (line,) = [json.loads(text) for text in err.splitlines() if text.strip()]
    assert line == {"event": "log_emit_failed", "logger": "lib", "level": "error"}


def test_warnings_are_logged_without_source_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    with warnings.catch_warnings():
        warnings.showwarning = warnings.showwarning  # keep ours inside pytest's capture
        configure_logging("INFO")
        warnings.showwarning(
            UserWarning(f"calling {SENSITIVE}"),
            UserWarning,
            "/app/src/gize_api/x.py",
            7,
            line="secret_source_line()",
        )
    (line,) = _lines(capsys)
    assert line["event"] == "python_warning"
    assert line["category"] == "UserWarning"
    assert line["location"] == "src/gize_api/x.py:7"
    serialized = json.dumps(line)
    assert "secret_source_line" not in serialized
    assert "0911223344" not in serialized
    assert "Abebe" not in serialized


async def test_error_escaping_our_middleware_never_reaches_spans_or_logs(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def broken(self: object, scope: object, receive: object, send: object) -> None:
        raise RuntimeError(f"outer {SENSITIVE}")

    monkeypatch.setattr(request_id_module.RequestIdMiddleware, "__call__", broken)
    spans = InMemorySpanExporter()
    app = create_app(make_settings())
    app.telemetry = configure_telemetry(make_settings(), span_exporter=spans)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/anything")
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    output = capsys.readouterr().err
    assert "Abebe" not in output
    for span in spans.get_finished_spans():
        assert not span.events
        assert "Abebe" not in str(span.status.description)


def test_record_without_arguments_passes_the_filter(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    record = logging.LogRecord("lib", logging.WARNING, "x.py", 1, "plain message", None, None)
    logging.getLogger("lib").handle(record)
    (line,) = _lines(capsys)
    assert line["event"] == "plain message"


def test_thread_system_exit_is_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    threading.excepthook(threading.ExceptHookArgs((SystemExit, SystemExit(0), None, None)))
    assert capsys.readouterr().err == ""


async def test_error_after_the_response_started_is_logged_not_answered_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gize_api.core.errors import LastResortGuard

    configure_logging("INFO")
    sent: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError(f"late {SENSITIVE}")

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await LastResortGuard(inner)({"type": "http", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]
    (line,) = _lines(capsys)
    assert line["event"] == "unhandled_error_outside_handlers"
    assert "Abebe" not in json.dumps(line)
