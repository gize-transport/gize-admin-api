"""GZ-066: every log line also goes to Grafana Cloud (Loki) over OTLP, exactly as written to
stderr: the same scrubbed line, its fields searchable, linked to the request's trace.

The OpenTelemetry libraries' own lines (an exporter failing, say) stay on stderr only, so a
failing export cannot feed itself; shipping never breaks logging.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gize_api import main
from gize_api.core import logging as gize_logging
from gize_api.core import telemetry as telemetry_module
from gize_api.core.config import Environment
from gize_api.core.logging import configure_logging, log_security_event
from gize_api.core.telemetry import Telemetry, configure_telemetry
from tests.support import SettingsFactory, memory_log_exporter

PHONE = "+251911223344"


@pytest.fixture
def exported() -> InMemoryLogRecordExporter:
    return memory_log_exporter()


@pytest.fixture
def shipping(
    make_settings: SettingsFactory, exported: InMemoryLogRecordExporter
) -> Iterator[Telemetry]:
    configure_logging("INFO")
    telemetry = configure_telemetry(
        make_settings(), span_exporter=InMemorySpanExporter(), log_exporter=exported
    )
    assert telemetry is not None
    telemetry.ship_logs()
    yield telemetry
    telemetry.stop_shipping_logs()


def _records(exported: InMemoryLogRecordExporter) -> list[ReadableLogRecord]:
    return list(exported.get_finished_logs())


def _one(exported: InMemoryLogRecordExporter, event: str) -> ReadableLogRecord:
    found = [r for r in _records(exported) if r.log_record.body == event]
    assert len(found) == 1, [r.log_record.body for r in _records(exported)]
    return found[0]


def test_a_line_is_shipped_as_written_with_its_fields(
    shipping: Telemetry,
    exported: InMemoryLogRecordExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_security_event("staff_token_rejected", sentry=False, reason="expired", phone=PHONE)
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    record = _one(exported, "staff_token_rejected").log_record
    attributes = dict(record.attributes or {})
    assert record.severity_number == SeverityNumber.WARN
    assert record.severity_text == "WARNING"
    assert attributes["category"] == "security"
    assert attributes["reason"] == "expired"
    assert attributes["logger"] == "gize_api.security"
    # The very scrubbed value stderr got: the phone masked, never in full.
    assert attributes["phone"] == line["phone"]
    assert PHONE not in json.dumps({k: str(v) for k, v in attributes.items()})
    assert record.timestamp is not None


def test_values_that_are_not_scalars_are_kept_readable(
    shipping: Telemetry, exported: InMemoryLogRecordExporter
) -> None:
    structlog.get_logger("gize_api.test").info(
        "shapes", count=3, ratio=0.5, ok=True, frames=["a.py:1", "b.py:2"], nested={"k": 1},
        missing=None, mixed=[1, "x"],
    )  # fmt: skip
    attributes = dict(_one(exported, "shapes").log_record.attributes or {})
    assert (attributes["count"], attributes["ratio"], attributes["ok"]) == (3, 0.5, True)
    assert attributes["frames"] == ("a.py:1", "b.py:2")
    assert json.loads(str(attributes["nested"])) == {"k": 1}
    assert json.loads(str(attributes["mixed"])) == [1, "x"]
    assert "missing" not in attributes


def test_a_line_inside_a_request_span_carries_its_trace(
    shipping: Telemetry, exported: InMemoryLogRecordExporter
) -> None:
    tracer = shipping.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        structlog.get_logger("gize_api.test").info("inside")
    record = _one(exported, "inside").log_record
    assert record.trace_id == span.get_span_context().trace_id
    assert record.span_id == span.get_span_context().span_id


@pytest.mark.parametrize("name", ["opentelemetry.exporter.otlp", "urllib3.connectionpool"])
def test_the_exporters_own_lines_stay_on_stderr(
    shipping: Telemetry,
    exported: InMemoryLogRecordExporter,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """An export that fails logs about it: shipping that line would feed the failure."""
    logging.getLogger(name).warning("Failed to export logs batch")
    assert "Failed to export logs batch" in capsys.readouterr().err
    assert _records(exported) == []


def test_a_failing_export_never_breaks_logging(
    shipping: Telemetry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_: Any) -> None:  # noqa: ANN401
        raise RuntimeError("collector gone")

    monkeypatch.setattr(gize_logging, "_export", broken)
    structlog.get_logger("gize_api.test").info("still_written")
    assert "still_written" in capsys.readouterr().err


def test_stopping_detaches_only_its_own_shipping(
    make_settings: SettingsFactory, exported: InMemoryLogRecordExporter
) -> None:
    configure_logging("INFO")
    first = configure_telemetry(make_settings(), log_exporter=exported)
    second = configure_telemetry(make_settings(), log_exporter=memory_log_exporter())
    assert first is not None
    assert second is not None
    first.ship_logs()
    second.stop_shipping_logs()  # not the one shipping: changes nothing
    structlog.get_logger("gize_api.test").info("kept")
    first.stop_shipping_logs()
    structlog.get_logger("gize_api.test").info("dropped")
    assert [r.log_record.body for r in _records(exported)] == ["kept"]


async def test_shutdown_stops_shipping_and_flushes(
    make_settings: SettingsFactory, exported: InMemoryLogRecordExporter
) -> None:
    configure_logging("INFO")
    telemetry = configure_telemetry(make_settings(), log_exporter=exported)
    assert telemetry is not None
    telemetry.ship_logs()
    structlog.get_logger("gize_api.test").info("before_shutdown")
    await telemetry.shutdown()
    structlog.get_logger("gize_api.test").info("after_shutdown")
    assert [r.log_record.body for r in _records(exported)] == ["before_shutdown"]
    assert gize_logging._export is None


def test_without_an_endpoint_nothing_is_shipped(make_settings: SettingsFactory) -> None:
    assert configure_telemetry(make_settings()) is None


async def test_the_otlp_log_exporter_uses_the_logs_path(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> InMemoryLogRecordExporter:  # noqa: ANN401
        made.append(kwargs)
        return memory_log_exporter()

    monkeypatch.setattr(telemetry_module, "OTLPLogExporter", capture)
    telemetry = configure_telemetry(
        make_settings(
            otel_exporter_otlp_endpoint="http://127.0.0.1:1",  # refused: nothing leaves
            otel_exporter_otlp_headers="Authorization=Basic eA==",
        )
    )
    assert telemetry is not None
    assert made == [
        {
            "endpoint": "http://127.0.0.1:1/v1/logs",
            "headers": {"Authorization": "Basic eA=="},
            "timeout": telemetry_module.EXPORT_TIMEOUT_SECONDS,
        }
    ]
    await telemetry.shutdown()


def test_startup_warnings_are_logged_once_shipping_runs(
    make_settings: SettingsFactory,
    exported: InMemoryLogRecordExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`observability_disabled` and `sms_disabled` alert (ops/grafana): they must reach Grafana."""
    real = telemetry_module.configure_telemetry
    made: list[Telemetry] = []

    def with_memory(settings: Any) -> Telemetry | None:  # noqa: ANN401
        telemetry = real(settings, log_exporter=exported)
        if telemetry is not None:
            made.append(telemetry)
        return telemetry

    monkeypatch.setattr("gize_api.main.configure_telemetry", with_memory)
    main.create_app(make_settings(app_env=Environment.STAGING, sentry_dsn=None,
                                  afromessage_token=None))  # fmt: skip
    try:
        bodies = [r.log_record.body for r in _records(exported)]
        assert "observability_disabled" in bodies
        assert "sms_disabled" in bodies
    finally:
        for telemetry in made:
            telemetry.stop_shipping_logs()


def test_without_a_log_provider_nothing_is_shipped(make_settings: SettingsFactory) -> None:
    """Telemetry for metrics only (as tests configure it) ships no logs."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    telemetry = configure_telemetry(make_settings(), metric_reader=InMemoryMetricReader())
    assert telemetry is not None
    assert telemetry.logger_provider is None
    telemetry.ship_logs()
    assert gize_logging.log_export() is None


def test_a_recursion_error_while_writing_is_not_swallowed(
    shipping: Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the standard library's handlers do: a recursion is a bug to see, not a lost line."""
    (handler,) = [
        h for h in logging.getLogger().handlers if isinstance(h, gize_logging._CurrentStderrHandler)
    ]

    def recurse(_: logging.LogRecord) -> str:
        raise RecursionError

    monkeypatch.setattr(handler, "format", recurse)
    with pytest.raises(RecursionError):
        handler.emit(logging.LogRecord("gize_api.test", logging.INFO, __file__, 1, "x", (), None))


async def test_non_http_traffic_passes_the_route_naming_untouched() -> None:
    from gize_api.core.telemetry import _NameSpanByRoute

    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        seen.append(scope["type"])

    async def nothing(*_: Any) -> Any:  # noqa: ANN401
        return None

    await _NameSpanByRoute(inner)({"type": "lifespan"}, nothing, nothing)
    assert seen == ["lifespan"]
