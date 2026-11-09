"""The tools pinned inside our workflows, compared with their latest releases (GZ-065).

Dependabot updates locked packages, actions, images and hooks, but not a version written in
a workflow's `env:`. The weekly pin-check workflow asks each tool's GitHub releases and
runs this; it prints a Markdown report of the stale pins (nothing when all are current),
which the workflow keeps as one issue.

    python -m scripts.stale_pins --latest uv=0.11.31 gitleaks=v8.30.1 oasdiff=v1.33.0
"""

import argparse
import re
import sys
from pathlib import Path

CI_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

# Each tool's GitHub repository: its latest release is the version to be on.
REPOS = {
    "uv": "astral-sh/uv",
    "gitleaks": "gitleaks/gitleaks",
    "oasdiff": "oasdiff/oasdiff",
}

# Where each pin is written: all of them move together (tests/test_ci_config.py checks).
WHERE = {
    "uv": "`UV_VERSION` in the three workflows, the Dockerfile's uv image, pyproject's "
    "`required-version`",
    "gitleaks": "`GITLEAKS_VERSION` and `GITLEAKS_SHA256` in ci.yml, the pre-commit hook",
    "oasdiff": "`OASDIFF_VERSION` and `OASDIFF_SHA256` in ci.yml and contract-release.yml",
}

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def pins(ci: str) -> dict[str, str]:
    """Each tool's pinned version, from ci.yml's `env:`."""
    found = {}
    for tool in REPOS:
        name = f"{tool.upper()}_VERSION"
        match = re.search(rf'^  {name}: "([\d.]+)"$', ci, re.MULTILINE)
        if match is None:
            raise ValueError(f"{name} is not pinned in ci.yml")
        found[tool] = match.group(1)
    return found


def _parts(tool: str, version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"{tool}: {version!r} is not a version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def stale(pinned: dict[str, str], latest: dict[str, str]) -> dict[str, tuple[str, str]]:
    """The pins behind their tool's latest release: tool -> (pinned, latest)."""
    behind = {}
    for tool, version in pinned.items():
        newest = _parts(tool, latest[tool])
        if newest > _parts(tool, version):
            behind[tool] = (version, ".".join(str(part) for part in newest))
    return behind


def report(behind: dict[str, tuple[str, str]]) -> str:
    """The issue's body, or "" when every pin is current."""
    if not behind:
        return ""
    rows = "\n".join(
        f"| {tool} | {pinned} | {newest} | {WHERE[tool]} |"
        for tool, (pinned, newest) in sorted(behind.items())
    )
    return (
        "The weekly pin check (GZ-065) found tools pinned inside our workflows that are "
        "behind their latest release. No update bot sees these pins.\n\n"
        "| Tool | Pinned | Latest | Where |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        'How to update them: RUNBOOK.md, "Dependency updates". This issue closes itself '
        "once every pin is current.\n"
    )


def _latest(values: list[str]) -> dict[str, str]:
    latest = {}
    for value in values:
        tool, sep, version = value.partition("=")
        if not sep or tool not in REPOS:
            raise argparse.ArgumentTypeError(f"expected tool=version for {', '.join(REPOS)}")
        latest[tool] = version
    if set(latest) != set(REPOS):
        raise argparse.ArgumentTypeError(f"expected tool=version for {', '.join(REPOS)}")
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ci", type=Path, default=CI_PATH)
    parser.add_argument("--latest", nargs="+", required=True, metavar="TOOL=VERSION")
    args = parser.parse_args(argv)
    try:
        latest = _latest(args.latest)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    sys.stdout.write(report(stale(pins(args.ci.read_text()), latest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
