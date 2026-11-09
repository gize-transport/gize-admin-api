"""GZ-065: the tools pinned inside the workflows, which no update bot can see.

scripts/stale_pins.py compares them with their latest releases; the weekly pin-check
workflow keeps one issue listing the stale ones.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.stale_pins import REPOS, pins, report, stale
from tests.support import ROOT

CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
PIN_CHECK = (ROOT / ".github" / "workflows" / "pin-check.yml").read_text()


def test_the_pins_are_read_from_ci() -> None:
    found = pins(CI)
    assert set(found) == set(REPOS) == {"uv", "gitleaks", "oasdiff"}
    assert all(version.count(".") == 2 for version in found.values())


def test_a_pin_missing_from_ci_is_an_error() -> None:
    with pytest.raises(ValueError, match="OASDIFF_VERSION"):
        pins('  UV_VERSION: "0.11.30"\n  GITLEAKS_VERSION: "8.30.1"\n')


def test_only_pins_behind_their_latest_release_are_stale() -> None:
    pinned = {"uv": "0.11.30", "gitleaks": "8.30.1", "oasdiff": "1.33.0"}
    latest = {"uv": "0.11.31", "gitleaks": "v8.30.1", "oasdiff": "v1.9.0"}  # 1.9 < 1.33
    assert stale(pinned, latest) == {"uv": ("0.11.30", "0.11.31")}


def test_a_newer_minor_is_stale_too() -> None:
    """uv 0.12 needs pyproject's range widened: a person decides, but they hear of it."""
    assert stale({"uv": "0.11.30"}, {"uv": "0.12.0"}) == {"uv": ("0.11.30", "0.12.0")}


def test_a_tag_that_is_not_a_version_is_an_error() -> None:
    with pytest.raises(ValueError, match="gitleaks"):
        stale({"gitleaks": "8.30.1"}, {"gitleaks": "nightly"})


def test_nothing_stale_reports_nothing() -> None:
    assert report({}) == ""


def test_the_report_says_where_each_pin_lives_and_how_to_update() -> None:
    text = report({"oasdiff": ("1.33.0", "1.34.0")})
    assert "| oasdiff | 1.33.0 | 1.34.0 |" in text
    assert "OASDIFF_SHA256" in text
    assert 'RUNBOOK.md, "Dependency updates"' in text


def test_the_workflow_asks_each_tool_s_own_releases() -> None:
    assert 'gh api "repos/$1/releases/latest"' in PIN_CHECK
    for tool, repo in REPOS.items():
        assert f'{tool}="$(latest {repo})"' in PIN_CHECK, tool
        assert f'"{tool}=${{{tool}}}"' in PIN_CHECK, tool


FIXED_CI = '  UV_VERSION: "0.11.30"\n  GITLEAKS_VERSION: "8.30.1"\n  OASDIFF_VERSION: "1.33.0"\n'


@pytest.mark.parametrize(
    ("latest", "stdout"),
    [
        (["uv=0.11.30", "gitleaks=v8.30.1", "oasdiff=v1.33.0"], ""),
        (
            ["uv=0.11.30", "gitleaks=v8.30.2", "oasdiff=v1.33.0"],
            "| gitleaks | 8.30.1 | 8.30.2 |",
        ),
    ],
)
def test_the_command_prints_the_report(tmp_path: Path, latest: list[str], stdout: str) -> None:
    ci = tmp_path / "ci.yml"
    ci.write_text(FIXED_CI)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-m", "scripts.stale_pins", "--ci", str(ci), "--latest", *latest],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (stdout in result.stdout) if stdout else result.stdout == ""


def test_the_command_refuses_a_malformed_latest() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.stale_pins", "--latest", "uv"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "tool=version" in result.stderr
