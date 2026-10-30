"""Release notes for each contract release (GZ-015 fix, found by GZ-021's release).

oasdiff lists a new error code once for every response of every operation; GZ-021's six
codes made notes too long to pass to the release step ("Argument list too long"). The
notes say each change once, and stay short however large the change. Inputs have the real
shapes: oasdiff 1.33's JSON changelog and `scripts.contract_codes`' keys.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.contract_codes import compare_codes
from scripts.contract_notes import MAX_LINE_BYTES, MAX_LINES, MAX_NOTES_BYTES, release_notes
from tests.support import ROOT

GZ021 = json.loads((ROOT / "tests" / "data" / "contract_notes_gz021.json").read_text())


def _contract(codes: list[str], operations: dict[str, list[str]]) -> dict[str, Any]:
    """A contract whose ErrorCode enum is `codes` and whose operations (`"PATCH /v1/a"`)
    may return the listed codes with a 409."""
    paths: dict[str, Any] = {}
    for operation, returned in operations.items():
        method, path = operation.split(" ")
        paths.setdefault(path, {})[method.lower()] = {
            "responses": {"409": {"x-error-codes": returned}} if returned else {}
        }
    return {"components": {"schemas": {"ErrorCode": {"enum": codes}}}, "paths": paths}


def _enum_line(
    code: str, operation: str, path: str, status: str, *, any_of: bool
) -> dict[str, str]:
    prop = "anyOf[subschema #1: ValidationProblem]/code" if any_of else "code"
    return {
        "id": "response-property-enum-value-added",
        "text": f"added the new `{code}` enum value to the `{prop}` response property for the"
        f" response status `{status}`",
        "operation": operation,
        "path": path,
    }


def test_gz021s_real_release_reads_as_what_changed() -> None:
    """The release that failed: six codes, four new endpoints, two new 409 responses."""
    notes = release_notes(GZ021["changelog"], GZ021["codes"])
    assert notes.splitlines() == [
        "**New error codes:** `COMPANY_IN_USE`, `CORRIDOR_IN_USE`, `ROUTE_CODE_TAKEN`,"
        " `ROUTE_HAS_RIDERS`, `SEATS_BELOW_LAUNCH_MINIMUM`, `SEATS_BELOW_TAKEN`",
        "",
        "**Error codes operations may now return:**",
        "- `DELETE /v1/companies/{company_id}`: 409 `COMPANY_IN_USE`",
        "- `DELETE /v1/corridors/{corridor_id}`: 409 `CORRIDOR_IN_USE`",
        "- `PATCH /v1/companies/{company_id}`: 409 `COMPANY_IN_USE`",
        "- `PATCH /v1/corridors/{corridor_id}`: 409 `CORRIDOR_IN_USE`",
        "",
        "**Changes:**",
        "- `DELETE /v1/companies/{company_id}`: added the non-success response with the"
        " status `409`",
        "- `DELETE /v1/corridors/{corridor_id}`: added the non-success response with the"
        " status `409`",
        "- `GET /v1/routes`: endpoint added",
        "- `POST /v1/routes`: endpoint added",
        "- `GET /v1/routes/{route_id}`: endpoint added",
        "- `PATCH /v1/routes/{route_id}`: endpoint added",
    ]


def test_a_new_code_listed_on_every_response_is_one_line() -> None:
    base = _contract(["A"], {f"PATCH /v1/t{n}": [] for n in range(40)})
    new = _contract(["A", "NEW_CODE"], {f"PATCH /v1/t{n}": [] for n in range(40)})
    changelog = [
        _enum_line("NEW_CODE", "PATCH", f"/v1/t{n}", status, any_of=any_of)
        for n in range(40)
        for status in ("401", "403", "422", "429", "500", "503")
        for any_of in (False, True)
    ]
    assert release_notes(changelog, compare_codes(base, new)) == "**New error codes:** `NEW_CODE`"


def test_other_enum_values_are_kept() -> None:
    """A new role or status is a change of its own, not an error code."""
    entry = {
        "id": "response-property-enum-value-added",
        "text": "added the new `paused` enum value to the `status` response property",
        "operation": "GET",
        "path": "/v1/routes",
    }
    assert "`paused`" in release_notes([entry], {"removed": [], "added": []})


def test_removed_codes_are_named_as_breaking() -> None:
    base = _contract(["OLD_CODE", "KEPT"], {"PATCH /v1/a": ["OLD_CODE", "KEPT"]})
    new = _contract(["KEPT"], {"PATCH /v1/a": ["KEPT"]})
    notes = release_notes([], compare_codes(base, new))
    assert notes.splitlines() == [
        "**Removed error codes (breaking):** `OLD_CODE`",
        "",
        "**Error codes operations no longer return (breaking):**",
        "- `PATCH /v1/a`: 409 `OLD_CODE`",
    ]


def test_repeated_changes_are_listed_once() -> None:
    entry = {
        "id": "x",
        "text": "added the optional property `notes`",
        "operation": "GET",
        "path": "/v1/a",
    }
    notes = release_notes([entry, entry, {**entry, "path": "/v1/b"}], {"removed": [], "added": []})
    assert notes.count("`notes`") == 2


def test_notes_stay_short_however_large_the_change() -> None:
    """Every section shares one budget: the per-operation codes too."""
    operations = {f"PATCH /v1/p{n}": ["A_CODE"] for n in range(3000)}
    codes = compare_codes(
        _contract(["A_CODE"], {op: [] for op in operations}), _contract(["A_CODE"], operations)
    )
    changelog = [
        {"id": f"c{n}", "text": "é" * 500, "operation": "GET", "path": f"/v1/p{n}"}
        for n in range(5000)
    ]
    notes = release_notes(changelog, codes)
    lines = notes.splitlines()
    assert len([line for line in lines if line.startswith("- ")]) <= MAX_LINES
    assert lines[-1].startswith("… and ")
    assert lines[-1].endswith("more (compare the two releases' openapi.json)")
    assert len(notes.encode()) <= MAX_NOTES_BYTES
    assert all(len(line.encode()) <= MAX_LINE_BYTES + 4 for line in lines)


def test_a_long_line_keeps_its_code_spans_closed() -> None:
    entry = {"id": "x", "text": "`" + "x" * 400 + "`", "operation": "GET", "path": "/v1/a"}
    (line,) = release_notes([entry], {"removed": [], "added": []}).splitlines()[1:]
    assert line.endswith("`…")
    assert line.count("`") % 2 == 0


def test_no_changes_say_so() -> None:
    assert release_notes([], {"removed": [], "added": []}) == (
        "Descriptions and examples only: no change to what the API accepts or returns."
    )


def _run(changelog: Path, codes: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [
            sys.executable,
            "-m",
            "scripts.contract_notes",
            "--changelog",
            str(changelog),
            "--codes",
            str(codes),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_the_command_prints_the_notes(tmp_path: Path) -> None:
    changelog, codes = tmp_path / "changelog.json", tmp_path / "codes.json"
    changelog.write_text(json.dumps(GZ021["changelog"]))
    codes.write_text(json.dumps(GZ021["codes"]))
    result = _run(changelog, codes)
    assert result.returncode == 0
    assert result.stdout.strip() == release_notes(GZ021["changelog"], GZ021["codes"])


@pytest.mark.parametrize(
    ("changelog", "codes"),
    [
        ("not json", '{"removed": [], "added": []}'),
        ('{"not": "a list"}', '{"removed": [], "added": []}'),
        ("[]", "nope"),
        ("[]", '{"removed": []}'),
        ("[]", '{"removed": [], "added": ["NOT_A_KEY"]}'),
        ('[{"text": 7}]', '{"removed": [], "added": []}'),
        (None, '{"removed": [], "added": []}'),  # a missing file
    ],
)
def test_the_command_refuses_bad_input(tmp_path: Path, changelog: str | None, codes: str) -> None:
    if changelog is not None:
        (tmp_path / "c.json").write_text(changelog)
    (tmp_path / "k.json").write_text(codes)
    result = _run(tmp_path / "c.json", tmp_path / "k.json")
    assert (result.returncode, result.stdout) == (2, "")
