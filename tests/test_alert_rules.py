"""GZ-066: the alert rules in ops/grafana, imported into Grafana Cloud (RUNBOOK, "Alerts").

Nothing in Grafana checks that a rule still matches what the API logs and measures: an event
renamed in the code would leave its alert silent forever. These tests hold every rule to
the code, the runbook and the shape Grafana's import expects.
"""

import ast
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


def _rule(kind: str, name: str) -> dict[str, Any]:
    (rule,) = [r for r in _rules(kind) if r["alert"] == name]
    return rule


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
    filters = re.findall(r'\|~ "\^\(?([a-z_|]+)\)?\$"', rule["expr"])
    assert filters, f"{rule['alert']}: no event filter"
    # A query counted more than once (a ratio, a volume floor) must count the same lines.
    assert len(set(filters)) == 1, rule["alert"]
    for event in filters[0].split("|"):
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
    (seen on staging, 2026-11-09). Only the silence rule reads metrics: counts of short
    bursts come from logs (GZ-070, below)."""
    expressions = " ".join(rule["expr"] for rule in _rules("metrics"))
    assert set(re.findall(r"([a-z_]+)\{", expressions)) == {"target_info"}


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


def test_the_5xx_alert_counts_request_lines_by_their_status() -> None:
    """GZ-070: from logs, not the request counter. A new metric series reaches Grafana with
    its first minute already counted, and `increase()` missed 61 of a burst's 68 503s
    (GZ-063). Every request line arrives as it happens.
    """
    expr = _rule("logs", "GizeApiServerErrors")["expr"]
    errors, everything = expr.split(" / ")
    # 5xx, all, and the volume floor: each the same lines, health probes left out (only
    # failed ones are logged; a new deploy failing them is no error users see).
    assert expr.count('|~ "^request$" | route!~"/health/.*"') == 3
    assert expr.count('|~ "^request$"') == 3
    # The status, the field the access log writes (a number), filters the 5xx count only.
    assert "| status >= 500" in errors
    assert "status" not in everything
    access = (ROOT / "src" / "gize_api" / "core" / "logging.py").read_text()
    assert re.search(r'log\(\s*"request",.{0,200}?\bstatus=status,', access, re.DOTALL)
    # Over 5% of at least 20 requests in 5 minutes: one 503 among a handful is no outage.
    assert re.search(r"\) > 0\.05\s+and on \(deployment_environment_name\)", expr)
    assert expr.rstrip().endswith("[5m])) >= 20")


def test_the_sms_alert_counts_every_failed_send() -> None:
    """GZ-070: from `sms_failed` lines, whatever the failure kind, so a new kind can't go
    unalerted. sms/service.py writes the line in one place, which every failure goes
    through; tests/sms/test_send_once.py checks each kind writes exactly one."""
    expr = _rule("logs", "GizeApiSmsFailing")["expr"]
    assert '|~ "^sms_failed$"' in expr
    assert "error" not in expr.replace("sms_failed", "")  # no filter on the kind
    assert expr.rstrip().endswith("[15m])) >= 3")
    assert SOURCE.count('"sms_failed"') == 1  # written in one place only: sms/service.py's _fail
    service = ast.parse((ROOT / "src" / "gize_api" / "sms" / "service.py").read_text())
    (fail,) = [
        f for f in ast.walk(service) if isinstance(f, ast.AsyncFunctionDef) and f.name == "_fail"
    ]
    calls = {
        call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else "": call
        for call in ast.walk(fail)
        if isinstance(call, ast.Call)
    }
    settle = [c for c in ast.walk(fail) if isinstance(c, ast.Name) and c.id == "_settle"]
    # Logged before the row is settled: a database failing then must not drop the line.
    assert calls["sms_failed"].lineno < min(name.lineno for name in settle)
