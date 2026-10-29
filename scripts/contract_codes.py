"""Compare the error codes of two contracts (GZ-015): what oasdiff cannot see.

    python -m scripts.contract_codes <base openapi.json> <new openapi.json>

Prints {"removed": [...], "added": [...]} and exits 1 if anything was removed. Error codes
are part of the contract (docs/api-contract.md: never renamed or reused, only added), but
oasdiff treats a removed `ErrorCode` value as harmless and does not read `x-error-codes`,
our own extension. Compared: the `ErrorCode` enum, and each operation's codes per status
(operations missing from either side are oasdiff's business).
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def compare_codes(base: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    old_codes, new_codes = _codes(base), _codes(new)
    one_sided = _one_sided_operations(base, new)
    return {
        "removed": sorted(old_codes - new_codes - one_sided),
        "added": sorted(new_codes - old_codes - one_sided),
    }


def _codes(contract: dict[str, Any]) -> set[str]:
    enum = contract.get("components", {}).get("schemas", {}).get("ErrorCode", {}).get("enum", [])
    codes = {f"ErrorCode {code}" for code in enum}
    for path, item in contract.get("paths", {}).items():
        for method, operation in _methods(item):
            for status, response in operation.get("responses", {}).items():
                for code in response.get("x-error-codes", []):
                    codes.add(f"{method.upper()} {path} {status} {code}")
    return codes


def _operations(contract: dict[str, Any]) -> set[str]:
    return {
        f"{method.upper()} {path} "
        for path, item in contract.get("paths", {}).items()
        for method, _ in _methods(item)
    }


def _methods(path_item: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The operations of a path item; `parameters`, `summary` and the like are not."""
    return [(key, value) for key, value in path_item.items() if key in HTTP_METHODS]


def _one_sided_operations(base: dict[str, Any], new: dict[str, Any]) -> set[str]:
    """Codes of operations that exist on one side only: oasdiff reports those operations."""
    one_sided = _operations(base) ^ _operations(new)
    return {code for code in _codes(base) | _codes(new) if code.startswith(tuple(one_sided))}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m scripts.contract_codes BASE NEW", file=sys.stderr)
        return 2
    try:
        base, new = (json.loads(Path(name).read_text()) for name in args)
        result = compare_codes(base, new)
    except Exception as exc:  # noqa: BLE001 - exit 1 means "removed": anything else is 2
        print(f"contract_codes: cannot compare the contracts: {exc!r}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 1 if result["removed"] else 0


if __name__ == "__main__":
    sys.exit(main())
