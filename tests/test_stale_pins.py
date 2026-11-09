"""GZ-065: the pins no Dependabot ecosystem sees (oasdiff, the Dockerfile frontend).

scripts/stale_pins.py compares them with what has been published for a few days; the
weekly pin-check workflow keeps one issue listing the stale ones.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.stale_pins import (
    FRONTEND_TAG,
    MIN_AGE,
    OASDIFF_RELEASES,
    frontend_pin,
    newest_release,
    oasdiff_pin,
    report,
    settled_digest,
    stale,
)
from tests.support import ROOT

CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
PIN_CHECK = (ROOT / ".github" / "workflows" / "pin-check.yml").read_text()
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
OLD = (NOW - MIN_AGE - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
NEW = (NOW - MIN_AGE + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _release(tag: str, published: str, **flags: bool) -> dict[str, Any]:
    return {"tag_name": tag, "published_at": published, "draft": False, "prerelease": False,
            **flags}  # fmt: skip


def test_the_pins_are_read_where_they_live() -> None:
    assert oasdiff_pin(CI).count(".") == 2
    assert frontend_pin(DOCKERFILE).startswith("sha256:")


def test_a_missing_pin_is_an_error() -> None:
    with pytest.raises(ValueError, match="OASDIFF_VERSION"):
        oasdiff_pin('  UV_VERSION: "0.13.7"\n')
    with pytest.raises(ValueError, match="first line"):
        frontend_pin("FROM python:3.12\n")


def test_the_newest_settled_release_counts() -> None:
    releases = [
        _release("v1.37.0", NEW),  # too new: like Dependabot's cooldown
        _release("v1.36.0", OLD, prerelease=True),
        _release("v1.40.0", OLD, draft=True),
        _release("nightly", OLD),
        _release("v1.9.0", OLD),  # 1.9 < 1.35: compared as numbers
        _release("v1.35.0", OLD),
    ]
    assert newest_release(releases, NOW) == "1.35.0"
    assert newest_release([_release("v1.37.0", NEW)], NOW) is None


def test_a_frontend_digest_counts_once_settled() -> None:
    assert settled_digest({"digest": OTHER, "tag_last_pushed": OLD}, NOW) == OTHER
    assert settled_digest({"digest": OTHER, "tag_last_pushed": NEW}, NOW) is None


@pytest.mark.parametrize(
    ("oasdiff", "frontend", "behind"),
    [
        (("1.33.0", "1.33.0"), (DIGEST, DIGEST), {}),
        (("1.33.0", None), (DIGEST, None), {}),  # nothing settled yet
        (("1.33.0", "1.35.0"), (DIGEST, DIGEST), {"oasdiff": ("1.33.0", "1.35.0")}),
        (("1.35.0", "1.33.0"), (DIGEST, OTHER), {"dockerfile frontend": (DIGEST, OTHER)}),
    ],
)
def test_only_what_is_behind_is_stale(
    oasdiff: tuple[str, str | None],
    frontend: tuple[str, str | None],
    behind: dict[str, tuple[str, str]],
) -> None:
    assert stale(oasdiff=oasdiff, frontend=frontend) == behind


def test_nothing_stale_reports_nothing() -> None:
    assert report({}) == ""


def test_the_report_says_where_each_pin_lives_and_how_to_update() -> None:
    text = report({"oasdiff": ("1.33.0", "1.35.0"), "dockerfile frontend": (DIGEST, OTHER)})
    assert "| oasdiff | `1.33.0` | `1.35.0` |" in text
    assert "OASDIFF_SHA256" in text
    assert "`# syntax=docker/dockerfile:1@…`" in text
    assert 'RUNBOOK.md, "Dependency updates"' in text


def test_the_workflow_fetches_what_the_script_reads() -> None:
    assert f'gh api "{OASDIFF_RELEASES}"' in PIN_CHECK
    assert f'"{FRONTEND_TAG}"' in PIN_CHECK
    assert "--oasdiff" in PIN_CHECK
    assert "--frontend" in PIN_CHECK


@pytest.mark.parametrize(
    ("frontend", "stale_text"), [(DIGEST, ""), (OTHER, "| dockerfile frontend |")]
)
def test_the_command_prints_the_report(tmp_path: Path, frontend: str, stale_text: str) -> None:
    (tmp_path / "ci.yml").write_text('  OASDIFF_VERSION: "1.35.0"\n')
    (tmp_path / "Dockerfile").write_text(f"# syntax=docker/dockerfile:1@{DIGEST}\nFROM x\n")
    (tmp_path / "releases.json").write_text(
        json.dumps([_release("v1.35.0", "2026-01-01T00:00:00Z")])
    )
    (tmp_path / "tag.json").write_text(
        json.dumps({"digest": frontend, "tag_last_pushed": "2026-01-01T00:00:00Z"})
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-m", "scripts.stale_pins", "--ci", str(tmp_path / "ci.yml"),
         "--dockerfile", str(tmp_path / "Dockerfile"), "--oasdiff", str(tmp_path / "releases.json"),
         "--frontend", str(tmp_path / "tag.json")],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert (stale_text in result.stdout) if stale_text else result.stdout == ""
