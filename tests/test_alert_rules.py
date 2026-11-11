"""GZ-066: the alert rules in ops/grafana, imported into Grafana Cloud (RUNBOOK, "Alerts").

Nothing in Grafana checks that a rule still matches what the API logs and measures: an event
renamed in the code would leave its alert silent forever. These tests hold every rule to
the code, the runbook and the shape Grafana's import expects.
"""

import re
from typing import Any, get_args

import pytest
import yaml

from gize_api.core.config import Settings
from gize_api.core.telemetry import _METRIC_ATTRIBUTES
from gize_api.integrations.sms import SmsFailure
from tests.support import ROOT

RULES = ROOT / "ops" / "grafana"
FILES = {"logs": RULES / "logs.rules.yaml", "metrics": RULES / "metrics.rules.yaml"}
RUNBOOK = (ROOT / "RUNBOOK.md").read_text()
SOURCE = "\n".join(path.read_text() for path in (ROOT / "src" / "gize_api").rglob("*.py"))
SERVICE = Settings.model_fields["otel_service_name"].default
# Production-only rules: staging sleeps when idle, so these would fire on it for nothing.
PRODUCTION_ONLY = {"GizeApiMisconfigured", "GizeApiSilent", "GizeApiSmsFailing"}
# Security events logged where nothing is shipped: the first-admin command (cli/), once, at
# setup. stderr and Sentry have them; an alert waiting for them would never fire.
NOT_SHIPPED = {"first_admin_created"}


def _rules(kind: str) -> list[dict[str, Any]]:
    document = yaml.safe_load(FILES[kind].read_text())
    (group,) = document["groups"]
    assert group["name"] == f"gize-api-{kind}"
    assert group["interval"] == "1m"
    return list(group["rules"])


ALL = [(kind, rule) for kind in FILES for rule in _rules(kind)]
NAMES = [rule["alert"] for _, rule in ALL]


def test_there_is_a_rule_for_each_thing_the_ticket_names() -> None:
    assert set(NAMES) == {
        "GizeApiSecurityEventsSpike",
        "GizeApiStaffAccessChanged",
        "GizeApiDependencyUnavailable",
        "GizeApiUnhandledErrors",
        "GizeApiMisconfigured",
        "GizeApiServerErrors",
        "GizeApiSilent",
        "GizeApiSmsFailing",
    }
    assert len(NAMES) == len(set(NAMES))


@pytest.mark.parametrize(("kind", "rule"), ALL, ids=NAMES)
def test_each_rule_is_complete(kind: str, rule: dict[str, Any]) -> None:
    assert set(rule) == {"alert", "expr", "for", "labels", "annotations"}
    assert re.fullmatch(r"\d+m", rule["for"])
    assert rule["labels"] == {"severity": rule["labels"]["severity"]}
    assert rule["labels"]["severity"] in {"critical", "warning"}
    annotations = rule["annotations"]
    assert set(annotations) == {"summary", "description", "runbook_url"}
    anchor = rule["alert"].lower()
    assert annotations["runbook_url"].endswith(f"/RUNBOOK.md#{anchor}")
    assert f"### {rule['alert']}\n" in RUNBOOK, "RUNBOOK must say what it means and what to do"


@pytest.mark.parametrize(("kind", "rule"), ALL, ids=NAMES)
def test_each_rule_looks_at_this_service(kind: str, rule: dict[str, Any]) -> None:
    selector = f'service_name="{SERVICE}"' if kind == "logs" else f'job="{SERVICE}"'
    assert selector in rule["expr"]


@pytest.mark.parametrize(("kind", "rule"), ALL, ids=NAMES)
def test_production_only_rules_say_so(kind: str, rule: dict[str, Any]) -> None:
    production = 'deployment_environment_name="production"' in rule["expr"]
    assert production == (rule["alert"] in PRODUCTION_ONLY)


@pytest.mark.parametrize(("kind", "rule"), ALL, ids=NAMES)
def test_rules_for_every_environment_count_each_apart_and_name_it(
    kind: str, rule: dict[str, Any]
) -> None:
    """Staging and production summed together would page for staging as for an outage."""
    if rule["alert"] in PRODUCTION_ONLY:
        return
    expr = rule["expr"]
    sums = re.findall(r"sum(?: by \(([^)]*)\))? \(", expr)
    assert sums
    for grouping in sums:
        assert "deployment_environment_name" in grouping, rule["alert"]
    if " and " in expr:
        assert "and on (deployment_environment_name)" in expr
    assert "{{ $labels.deployment_environment_name }}" in rule["annotations"]["summary"]


@pytest.mark.parametrize("rule", _rules("logs"), ids=[r["alert"] for r in _rules("logs")])
def test_every_event_a_log_rule_waits_for_is_one_the_code_logs(rule: dict[str, Any]) -> None:
    """An event renamed in the code must not leave its alert silent."""
    (events,) = re.findall(r'\|~ "\^\(?([a-z_|]+)\)?\$"', rule["expr"])
    for event in events.split("|"):
        # As a call's first argument (`logger.error("unhandled_error", …)`), not any mention.
        logged = re.search(rf'\(\s*"{event}"|"{event}" if |else "{event}"', SOURCE)
        assert logged, f"{rule['alert']}: nothing logs {event!r}"


def test_the_security_rules_cover_every_security_event() -> None:
    """A new `log_security_event(...)` must be put in one of the two security alerts."""
    logged = set(re.findall(r'log_security_event\(\s*"([a-z_]+)"', SOURCE))
    logged |= set(re.findall(r'_denied\(\s*"([a-z_]+)"', SOURCE))
    logged |= set(re.findall(r'"(staff_reactivated)" if', SOURCE))
    logged |= set(re.findall(r'else "(staff_deactivated)"', SOURCE))
    covered = set()
    for rule in _rules("logs"):
        if 'category="security"' in rule["expr"]:
            (events,) = re.findall(r'\|~ "\^\(([a-z_|]+)\)\$"', rule["expr"])
            covered |= set(events.split("|"))
    assert logged - NOT_SHIPPED == covered
    assert logged >= NOT_SHIPPED


def test_metric_rules_use_the_names_grafana_stores() -> None:
    """OTLP metrics land in Grafana Cloud with dots as underscores and the unit appended
    (seen on staging, 2026-11-09): `http.server.duration` (ms) is the one below."""
    expressions = " ".join(rule["expr"] for rule in _rules("metrics"))
    # The status label the rules read is one the metrics keep (core/telemetry.py's View).
    assert "http.status_code" in _METRIC_ATTRIBUTES
    assert "http_status_code" in expressions
    assert set(re.findall(r"([a-z_]+)\{", expressions)) == {
        "gize_sms_send_duration_seconds_count",
        "http_server_duration_milliseconds_count",
        "target_info",
    }


def test_the_files_say_how_to_import_them() -> None:
    for kind, path in FILES.items():
        header = path.read_text().split("\ngroups:", 1)[0]
        assert f"grafanacloud-<stack>-{'logs' if kind == 'logs' else 'prom'}" in header
        assert 'RUNBOOK.md, "Alerts"' in header


def test_files_are_where_the_runbook_says() -> None:
    for path in FILES.values():
        assert f"ops/grafana/{path.name}" in RUNBOOK


def test_routers_carry_their_own_prefixes() -> None:
    """Spans, access logs and metrics name a request by `scope["route"]`, which FastAPI 0.162
    sets to the router's own route: a prefix added at `include_router` would be missing from
    all three (and from the alert queries that split by route)."""
    main = (ROOT / "src" / "gize_api" / "main.py").read_text()
    calls = re.findall(r"app\.include_router\(([^)]*)\)", main)
    assert calls
    assert all("prefix" not in call for call in calls)


def test_the_sms_alert_counts_every_failure_kind_and_only_those() -> None:
    """GZ-063: a new way for an SMS to fail must not go unalerted."""
    (rule,) = [r for r in _rules("metrics") if r["alert"] == "GizeApiSmsFailing"]
    (kinds,) = re.findall(r'outcome=~"([a-z|]+)"', rule["expr"])
    assert set(kinds.split("|")) == set(get_args(SmsFailure))
    assert "gize.sms.send.duration" in SOURCE  # the metric the rule reads, by its OTel name
