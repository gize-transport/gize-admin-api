"""Release notes for each contract release (GZ-015 fix, found by GZ-021's release).

oasdiff lists a new error code once for every response of every operation; GZ-021's six
codes made notes too long to pass to the release step ("Argument list too long"). The
notes say each change once, and stay short however large the change.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.contract_notes import MAX_CHANGE_LINES, MAX_NOTES_CHARS, release_notes
from tests.support import ROOT


def _code_entry(code: str, operation: str, path: str, status: str) -> dict[str, object]:
    return {
        "id": "response-property-enum-value-added",
        "text": (
            f"added the new `{code}` enum value to the `code` response property for the"
            f" response status `{status}`"
        ),
        "level": 1,
        "operation": operation,
        "path": path,
    }


def test_new_error_codes_are_one_line_however_many_responses_list_them() -> None:
    codes = ["ROUTE_CODE_TAKEN", "SEATS_BELOW_TAKEN", "CORRIDOR_IN_USE"]
    changelog = [
        _code_entry(code, method, f"/v1/thing{n}", status)
        for code in codes
        for n in range(40)
        for method in ("GET", "PATCH")
        for status in ("401", "403", "422", "429", "500", "503")
    ]
    changelog.append(
        {
            "id": "endpoint-added",
            "text": "endpoint added",
            "operation": "POST",
            "path": "/v1/routes",
        }
    )
    notes = release_notes(changelog, {"removed": [], "added": codes})
    assert notes.splitlines() == [
        "**New error codes:** `CORRIDOR_IN_USE`, `ROUTE_CODE_TAKEN`, `SEATS_BELOW_TAKEN`",
        "",
        "**Changes:**",
        "- `POST /v1/routes`: endpoint added",
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


def test_other_enum_values_are_kept() -> None:
    """A new role or status is a change of its own, not an error code."""
    entry = {
        "id": "response-property-enum-value-added",
        "text": "added the new `paused` enum value to the `status` response property",
        "operation": "GET",
        "path": "/v1/routes",
    }
    assert "`paused`" in release_notes([entry], {"removed": [], "added": []})


def test_removed_error_codes_are_named() -> None:
    notes = release_notes([], {"removed": ["OLD_CODE"], "added": []})
    assert notes.startswith("**Removed error codes (breaking):** `OLD_CODE`")


def test_notes_stay_short_however_large_the_change() -> None:
    changelog = [
        {"id": f"c{n}", "text": "x" * 500, "operation": "GET", "path": f"/v1/p{n}"}
        for n in range(5000)
    ]
    notes = release_notes(changelog, {"removed": [], "added": []})
    lines = notes.splitlines()
    assert len([line for line in lines if line.startswith("- ")]) <= MAX_CHANGE_LINES + 1
    assert lines[-1].startswith("- … and ")
    assert lines[-1].endswith("more (compare the two releases' openapi.json)")
    assert len(notes) <= MAX_NOTES_CHARS


def test_no_changes_say_so() -> None:
    assert release_notes([], {"removed": [], "added": []}) == (
        "Descriptions and examples only: no change to what the API accepts or returns."
    )


def test_the_command_prints_the_notes(tmp_path: Path) -> None:
    changelog, codes = tmp_path / "changelog.json", tmp_path / "codes.json"
    changelog.write_text(json.dumps([_code_entry("NEW_CODE", "GET", "/v1/a", "422")]))
    codes.write_text(json.dumps({"removed": [], "added": ["NEW_CODE"]}))
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
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
    assert (result.returncode, result.stdout.strip()) == (0, "**New error codes:** `NEW_CODE`")


@pytest.mark.parametrize(
    ("changelog", "codes"),
    [
        ("not json", '{"removed": [], "added": []}'),
        ('{"not": "a list"}', '{"removed": [], "added": []}'),
        ("[]", "nope"),
        ("[]", '{"removed": []}'),
        ('[{"text": 7}]', '{"removed": [], "added": []}'),
    ],
)
def test_the_command_refuses_bad_input(tmp_path: Path, changelog: str, codes: str) -> None:
    (tmp_path / "c.json").write_text(changelog)
    (tmp_path / "k.json").write_text(codes)
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [
            sys.executable,
            "-m",
            "scripts.contract_notes",
            "--changelog",
            str(tmp_path / "c.json"),
            "--codes",
            str(tmp_path / "k.json"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
