"""Release notes for a contract release (the release workflow, GZ-015).

    python -m scripts.contract_notes --changelog <file> --codes <file>

`--changelog` is `oasdiff changelog --format json` between the two releases; `--codes` is
`python -m scripts.contract_codes`. oasdiff lists a new error code once for every response
of every operation (GZ-021's six codes made thousands of lines, past what a workflow step
can be given), so the notes:
- name new and removed error codes once;
- list every other change once (operation, path, what changed);
- stay short: at most MAX_CHANGE_LINES changes and MAX_NOTES_CHARS characters, then
  "… and N more" (the full difference is between the two releases' openapi.json).

Exit codes: 0 notes printed, 2 unreadable input (nothing printed).
"""

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_CHANGE_LINES = 200
MAX_LINE_CHARS = 300
MAX_NOTES_CHARS = 60_000
NO_CHANGES = "Descriptions and examples only: no change to what the API accepts or returns."
_NEW_ERROR_CODE = re.compile(r"added the new `([A-Z0-9_]+)` enum value to the `code` response")


def release_notes(
    changelog: Sequence[Mapping[str, Any]], codes: Mapping[str, Sequence[str]]
) -> str:
    sections: list[str] = []
    if codes["removed"]:
        sections.append(f"**Removed error codes (breaking):** {_names(codes['removed'])}")
    if codes["added"]:
        sections.append(f"**New error codes:** {_names(codes['added'])}")
    added = set(codes["added"])
    changes: list[str] = []
    for entry in changelog:
        text = str(entry.get("text", "")).strip()
        code = _NEW_ERROR_CODE.search(text)
        if code is not None and code.group(1) in added:
            continue  # named once above
        where = " ".join(str(entry.get(key, "")) for key in ("operation", "path")).strip()
        line = f"- `{where}`: {text}" if where else f"- {text}"
        line = line[:MAX_LINE_CHARS]
        if line not in changes:
            changes.append(line)
    if changes:
        listed, size = [], 0
        for line in changes[:MAX_CHANGE_LINES]:
            if size + len(line) > MAX_NOTES_CHARS - 1000:  # room for the rest
                break
            listed.append(line)
            size += len(line) + 1
        if len(listed) < len(changes):
            more = len(changes) - len(listed)
            listed.append(f"- … and {more} more (compare the two releases' openapi.json)")
        sections.append("**Changes:**\n" + "\n".join(listed))
    return "\n\n".join(sections) if sections else NO_CHANGES


def _names(codes: Sequence[str]) -> str:
    return ", ".join(f"`{code}`" for code in sorted(codes))


def _changelog(text: str) -> list[Mapping[str, Any]]:
    changes = json.loads(text) if text.strip() else []
    if changes is None:
        return []
    if not isinstance(changes, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("text", ""), str) for entry in changes
    ):
        raise ValueError("unexpected changelog: not a list of changes")
    return changes


def _codes(text: str) -> dict[str, list[str]]:
    codes = json.loads(text)
    if not isinstance(codes, dict) or not all(
        isinstance(codes.get(key), list) for key in ("removed", "added")
    ):
        raise ValueError("unexpected codes comparison: needs removed and added lists")
    return codes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--changelog", required=True, type=Path)
    parser.add_argument("--codes", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        notes = release_notes(
            _changelog(args.changelog.read_text()), _codes(args.codes.read_text())
        )
    except ValueError as exc:  # json.JSONDecodeError included
        print(f"contract_notes: {exc}", file=sys.stderr)
        return 2
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
