"""The next api-vX.Y.Z tag for a changed contract (the release workflow, GZ-015).

    python -m scripts.contract_version --previous <tag or ""> --breaking 0|1 \\
        --changelog <file> --codes <file>

- no previous release: api-v1.0.0;
- breaking changes (only with the `breaking-change-approved` label): the next major;
- any change oasdiff reports (an added endpoint, field or enum value), or an added error
  code: the next minor;
- otherwise (descriptions, examples, summaries): the next patch.

`--changelog` is `oasdiff changelog --format json` between the two releases; `--codes` is
`python -m scripts.contract_codes` (a removed error code is breaking, an added one a change).
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

TAG = re.compile(r"api-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
FIRST = "api-v1.0.0"


def next_version(previous: str | None, *, breaking: bool, changed: bool) -> str:
    if previous is None:
        return FIRST
    match = TAG.fullmatch(previous)
    if match is None:
        raise ValueError(f"unexpected previous tag: {previous!r}")
    major, minor, patch = (int(part) for part in match.groups())
    if breaking:
        return f"api-v{major + 1}.0.0"
    if changed:
        return f"api-v{major}.{minor + 1}.0"
    return f"api-v{major}.{minor}.{patch + 1}"


def changelog_entries(text: str) -> int:
    """How many changes oasdiff listed (it prints `null` or `[]` for none)."""
    if not text.strip():
        return 0
    try:
        changes = json.loads(text)
    except ValueError:
        raise ValueError("unreadable changelog") from None
    if changes is None:
        return 0
    if not isinstance(changes, list):
        raise ValueError("unexpected changelog: not a list of changes")
    return len(changes)


def code_changes(text: str) -> tuple[int, int]:
    """(removed, added) from scripts.contract_codes' output."""
    try:
        codes = json.loads(text)
    except ValueError:
        raise ValueError("unreadable codes comparison") from None
    if not isinstance(codes, dict) or not all(
        isinstance(codes.get(key), list) for key in ("removed", "added")
    ):
        raise ValueError("unexpected codes comparison: needs removed and added lists")
    return len(codes["removed"]), len(codes["added"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--previous", required=True, help='the last api-v* tag, or ""')
    parser.add_argument("--breaking", required=True, choices=["0", "1"])
    parser.add_argument("--changelog", required=True, type=Path)
    parser.add_argument("--codes", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        removed, added = code_changes(args.codes.read_text())
        changed = changelog_entries(args.changelog.read_text()) > 0 or added > 0
        breaking = args.breaking == "1" or removed > 0
        tag = next_version(args.previous or None, breaking=breaking, changed=changed)
    except ValueError as exc:
        print(f"contract_version: {exc}", file=sys.stderr)
        return 2
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
