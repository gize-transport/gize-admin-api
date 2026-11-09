"""GZ-066: the alert rules in ops/grafana, imported into Grafana Cloud (RUNBOOK, "Alerts").

Nothing in Grafana checks that a rule still matches what the API logs and measures: an event
renamed in the code would leave its alert silent forever. These tests hold every rule to
the code, the runbook and the shape Grafana's import expects.
"""

import re
from typing import Any

import pytest
import yaml

from gize_api.core.config import Settings
from tests.support import ROOT

RULES = ROOT / "ops" / "grafana"
FILES = {"logs": RULES / "logs.rules.yaml", "metrics": RULES / "metrics.rules.yaml"}
RUNBOOK = (ROOT / "RUNBOOK.md").read_text()
SOURCE = "\n".join(path.read_text() for path in (ROOT / "src" / "gize_api").rglob("*.py"))
SERVICE = Settings.model_fields["otel_service_name"].default
# Production-only rules: staging sleeps when idle, so these would fire on it for nothing.
PRODUCTION_ONLY = {"GizeApiMisconfigured", "GizeApiSilent"}


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


@pytest.mark.parametrize("rule", _rules("logs"), ids=[r["alert"] for r in _rules("logs")])
def test_every_event_a_log_rule_waits_for_is_one_the_code_logs(rule: dict[str, Any]) -> None:
    """An event renamed in the code must not leave its alert silent."""
    (events,) = re.findall(r'\|~ "\^\(?([a-z_|]+)\)?\$"', rule["expr"])
    for event in events.split("|"):
        assert re.search(rf'"{event}"', SOURCE), f"{rule['alert']}: nothing logs {event!r}"


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
    assert logged == covered


def test_metric_rules_use_the_names_grafana_stores() -> None:
    """OTLP metrics land in Grafana Cloud with dots as underscores and the unit appended
    (seen on staging, 2026-11-09): `http.server.duration` (ms) is the one below."""
    expressions = " ".join(rule["expr"] for rule in _rules("metrics"))
    assert set(re.findall(r"([a-z_]+)\{", expressions)) == {
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
