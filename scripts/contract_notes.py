"""Release notes for a contract release (the release workflow, GZ-015).

    python -m scripts.contract_notes --changelog <file> --codes <file>

`--changelog` is `oasdiff changelog --format json` between the two releases; `--codes` is
`python -m scripts.contract_codes`, whose keys are `ErrorCode X` (a code in the enum) and
`METHOD /path STATUS X` (a code an operation may return). oasdiff also reports every new
enum value once per response of every operation (GZ-021's six codes made 996 such lines,
too long to hand to the release step), so the notes:
- name new and removed error codes once;
- list which operations gained or lost which codes, one line per operation;
- list every other change once (operation, path, what changed);
- stay short: at most MAX_LINES lines and MAX_NOTES_BYTES, then "… and N more" (the full
  difference is between the two releases' openapi.json).

Exit codes: 0 notes printed, 2 unreadable input (nothing printed).
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

MAX_LINES = 200
MAX_LINE_BYTES = 300
MAX_NOTES_BYTES = 60_000  # far below a step's 128 KB argument limit
NO_CHANGES = "Descriptions and examples only: no change to what the API accepts or returns."
# The `code` property of a problem, plain or inside an anyOf (oasdiff 1.33's two wordings).
_NEW_ENUM_CODE = re.compile(
    r"added the new `([A-Z0-9_]+)` enum value to the `(?:[^`]*/)?code` response property"
)
_ENUM_KEY = "ErrorCode "


def release_notes(
    changelog: Sequence[Mapping[str, Any]], codes: Mapping[str, Sequence[str]]
) -> str:
    added, removed = _split(codes["added"]), _split(codes["removed"])
    new_codes = set(added.enum)
    heads: list[str] = []
    if removed.enum:
        heads.append(f"**Removed error codes (breaking):** {_names(removed.enum)}")
    if added.enum:
        heads.append(f"**New error codes:** {_names(added.enum)}")
    sections: list[tuple[str, list[str]]] = []
    if removed.per_operation:
        sections.append(
            ("**Error codes operations no longer return (breaking):**", removed.per_operation)
        )
    if added.per_operation:
        sections.append(("**Error codes operations may now return:**", added.per_operation))
    changes: list[str] = []
    for entry in changelog:
        text = str(entry.get("text", "")).strip()
        code = _NEW_ENUM_CODE.search(text)
        if code is not None and code.group(1) in new_codes:
            continue  # the new code is named once above
        where = " ".join(str(entry.get(key, "")) for key in ("operation", "path")).strip()
        line = _short(f"- `{where}`: {text}" if where else f"- {text}")
        if line not in changes:
            changes.append(line)
    if changes:
        sections.append(("**Changes:**", changes))
    return _within_budget(heads, sections) or NO_CHANGES


class _Codes:
    def __init__(self, enum: list[str], per_operation: list[str]) -> None:
        self.enum, self.per_operation = enum, per_operation


def _split(keys: Iterable[str]) -> _Codes:
    """`ErrorCode X` keys, and `METHOD /path STATUS X` keys grouped per operation."""
    enum: list[str] = []
    operations: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        if key.startswith(_ENUM_KEY):
            enum.append(key.removeprefix(_ENUM_KEY))
            continue
        method, path, status, code = key.split(" ")
        operations[f"{method} {path}"].append(f"{status} `{code}`")
    lines = [
        _short(f"- `{operation}`: {', '.join(sorted(found))}")
        for operation, found in sorted(operations.items())
    ]
    return _Codes(enum, lines)


def _within_budget(heads: list[str], sections: list[tuple[str, list[str]]]) -> str:
    """The heads, then each section's lines while the budget lasts; the rest is counted."""
    parts, used, lines_used, left_out = list(heads), _size(heads), 0, 0
    for title, lines in sections:
        listed: list[str] = []
        for line in lines:
            if lines_used >= MAX_LINES or used + _size([line]) > MAX_NOTES_BYTES - 1000:
                left_out += 1
                continue
            listed.append(line)
            used += _size([line])
            lines_used += 1
        if listed:
            parts.append("\n".join([title, *listed]))
    if left_out:
        parts.append(f"… and {left_out} more (compare the two releases' openapi.json)")
    return "\n\n".join(parts)


def _size(lines: Iterable[str]) -> int:
    return sum(len(line.encode()) + 1 for line in lines)


def _short(line: str) -> str:
    """At most MAX_LINE_BYTES, cut on a character, with code spans kept closed."""
    if len(line.encode()) <= MAX_LINE_BYTES:
        return line
    cut = line.encode()[: MAX_LINE_BYTES - 8].decode(errors="ignore")
    return cut + ("`" if cut.count("`") % 2 else "") + "…"


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
        isinstance(codes.get(key), list)
        and all(
            isinstance(item, str) and (item.startswith(_ENUM_KEY) or len(item.split(" ")) == 4)
            for item in codes[key]
        )
        for key in ("removed", "added")
    ):
        raise ValueError("unexpected codes comparison: needs removed and added lists of keys")
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
    except (OSError, ValueError) as exc:  # a missing file; json.JSONDecodeError included
        print(f"contract_notes: {exc}", file=sys.stderr)
        return 2
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
