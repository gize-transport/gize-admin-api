"""GZ-015: error codes are part of the contract, but oasdiff cannot see them all.

Removing an ErrorCode value is "INFO" to oasdiff, and `x-error-codes` is our own extension,
so scripts/contract_codes.py compares them: a removal is breaking, an addition is a change.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.contract_codes import compare_codes
from tests.support import ROOT


def _contract(codes: list[str], operations: dict[str, list[str]]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for key, operation_codes in operations.items():
        method, path = key.split(" ")
        paths.setdefault(path, {})[method.lower()] = {
            "responses": {
                "200": {"description": "OK"},
                "403": {"x-error-codes": operation_codes},
            }
        }
    return {"components": {"schemas": {"ErrorCode": {"enum": codes}}}, "paths": paths}


BASE = _contract(["FORBIDDEN", "NOT_FOUND"], {"GET /v1/a": ["FORBIDDEN"]})


def test_the_same_codes_are_no_change() -> None:
    assert compare_codes(BASE, BASE) == {"removed": [], "added": []}


def test_a_removed_error_code_is_reported() -> None:
    new = _contract(["FORBIDDEN"], {"GET /v1/a": ["FORBIDDEN"]})
    assert compare_codes(BASE, new)["removed"] == ["ErrorCode NOT_FOUND"]


def test_a_code_dropped_from_an_operation_is_reported() -> None:
    new = _contract(["FORBIDDEN", "NOT_FOUND"], {"GET /v1/a": []})
    assert compare_codes(BASE, new)["removed"] == ["GET /v1/a 403 FORBIDDEN"]


def test_additions_are_reported() -> None:
    new = _contract(
        ["CONFLICT", "FORBIDDEN", "NOT_FOUND"],
        {"GET /v1/a": ["FORBIDDEN", "NOT_FOUND"], "POST /v1/b": ["FORBIDDEN"]},
    )
    assert compare_codes(BASE, new) == {
        "removed": [],
        "added": ["ErrorCode CONFLICT", "GET /v1/a 403 NOT_FOUND"],
    }


def test_a_removed_operation_is_left_to_oasdiff() -> None:
    new = _contract(["FORBIDDEN", "NOT_FOUND"], {})
    assert compare_codes(BASE, new)["removed"] == []


def test_the_real_contract_against_itself() -> None:
    contract = json.loads((ROOT / "openapi.json").read_text())
    assert compare_codes(contract, contract) == {"removed": [], "added": []}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-m", "scripts.contract_codes", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=ROOT,
    )


@pytest.mark.parametrize(
    ("new", "code"),
    [
        (BASE, 0),
        (_contract(["FORBIDDEN"], {"GET /v1/a": ["FORBIDDEN"]}), 1),
    ],
)
def test_the_command_fails_on_removals(tmp_path: Path, new: dict[str, Any], code: int) -> None:
    base_file, new_file = tmp_path / "base.json", tmp_path / "new.json"
    base_file.write_text(json.dumps(BASE))
    new_file.write_text(json.dumps(new))
    result = _run(str(base_file), str(new_file))
    assert result.returncode == code
    assert set(json.loads(result.stdout)) == {"removed", "added"}


def test_the_command_refuses_unreadable_files(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("nope")
    result = _run(str(bad), str(bad))
    assert result.returncode == 2
    assert "contract_codes" in result.stderr
