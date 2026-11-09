"""The pins no Dependabot ecosystem sees, compared with what is published (GZ-065).

Dependabot updates uv.lock, actions, images, Compose services and pre-commit hooks; a
version it updates elsewhere (the uv image, the gitleaks hook) fails CI until the workflow
pins follow (tests/test_ci_config.py). Two pins have no ecosystem at all: oasdiff, written
in two workflows, and the Dockerfile's `# syntax` frontend. The weekly pin-check workflow
fetches what is published and runs this; it prints a Markdown report of the stale ones
(nothing when all are current), which the workflow keeps as one issue.

Like Dependabot's cooldown, a release or a tag push must be MIN_AGE old to count.

    python -m scripts.stale_pins --oasdiff releases.json --frontend tag.json
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
MIN_AGE = timedelta(days=5)  # .github/dependabot.yml's cooldown

OASDIFF_RELEASES = "repos/oasdiff/oasdiff/releases?per_page=30"
FRONTEND_TAG = "https://hub.docker.com/v2/repositories/docker/dockerfile/tags/1"

WHERE = {
    "oasdiff": "`OASDIFF_VERSION` and `OASDIFF_SHA256` in ci.yml and contract-release.yml",
    "dockerfile frontend": "the Dockerfile's first line (`# syntax=docker/dockerfile:1@…`)",
}

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_FRONTEND = re.compile(r"\A# syntax=docker/dockerfile:1@(sha256:[0-9a-f]{64})\n")


def _parts(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"{version!r} is not a version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def oasdiff_pin(ci: str) -> str:
    match = re.search(r'^  OASDIFF_VERSION: "([\d.]+)"$', ci, re.MULTILINE)
    if match is None:
        raise ValueError("OASDIFF_VERSION is not pinned in ci.yml")
    return match.group(1)


def frontend_pin(dockerfile: str) -> str:
    match = _FRONTEND.match(dockerfile)
    if match is None:
        raise ValueError("the Dockerfile's first line is not a digest-pinned dockerfile:1")
    return match.group(1)


def newest_release(releases: list[dict[str, Any]], now: datetime) -> str | None:
    """The newest published, non-prerelease release at least MIN_AGE old (its version)."""
    settled = [
        _parts(release["tag_name"])
        for release in releases
        if not release["draft"]
        and not release["prerelease"]
        and _VERSION.fullmatch(release["tag_name"])
        and now - _when(release["published_at"]) >= MIN_AGE
    ]
    return ".".join(str(part) for part in max(settled)) if settled else None


def settled_digest(tag: dict[str, Any], now: datetime) -> str | None:
    """The tag's digest, once it has been MIN_AGE since it was last pushed."""
    if now - _when(tag["tag_last_pushed"]) < MIN_AGE:
        return None
    return str(tag["digest"])


def stale(
    *, oasdiff: tuple[str, str | None], frontend: tuple[str, str | None]
) -> dict[str, tuple[str, str]]:
    """What is behind: name -> (pinned, published). Each argument is (pinned, published)."""
    behind = {}
    pinned, newest = oasdiff
    if newest is not None and _parts(newest) > _parts(pinned):
        behind["oasdiff"] = (pinned, newest)
    pinned, newest = frontend
    if newest is not None and newest != pinned:
        behind["dockerfile frontend"] = (pinned, newest)
    return behind


def report(behind: dict[str, tuple[str, str]]) -> str:
    """The issue's body, or "" when every pin is current."""
    if not behind:
        return ""
    rows = "\n".join(
        f"| {name} | `{pinned}` | `{newest}` | {WHERE[name]} |"
        for name, (pinned, newest) in sorted(behind.items())
    )
    return (
        "The weekly pin check (GZ-065) found pins no Dependabot ecosystem sees that are "
        f"behind what has been published for at least {MIN_AGE.days} days.\n\n"
        "| Pin | Pinned | Published | Where |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        'How to update them: RUNBOOK.md, "Dependency updates". This issue closes itself '
        "once every pin is current.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oasdiff", type=Path, required=True, help=f"gh api {OASDIFF_RELEASES}")
    parser.add_argument("--frontend", type=Path, required=True, help=f"curl {FRONTEND_TAG}")
    parser.add_argument("--ci", type=Path, default=CI_PATH)
    parser.add_argument("--dockerfile", type=Path, default=DOCKERFILE_PATH)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    behind = stale(
        oasdiff=(
            oasdiff_pin(args.ci.read_text()),
            newest_release(json.loads(args.oasdiff.read_text()), now),
        ),
        frontend=(
            frontend_pin(args.dockerfile.read_text()),
            settled_digest(json.loads(args.frontend.read_text()), now),
        ),
    )
    sys.stdout.write(report(behind))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
