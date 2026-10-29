"""GZ-015: the version of each released contract (api-vX.Y.Z), from what changed."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.contract_version import changelog_entries, next_version
from tests.support import ROOT


@pytest.mark.parametrize(
    ("previous", "breaking", "changed", "expected"),
    [
        (None, False, False, "api-v1.0.0"),  # the first release
        (None, True, True, "api-v1.0.0"),
        ("api-v1.0.0", False, True, "api-v1.1.0"),  # additive
        ("api-v1.4.2", False, True, "api-v1.5.0"),
        ("api-v1.4.2", False, False, "api-v1.4.3"),  # descriptions, examples
        ("api-v1.4.2", True, True, "api-v2.0.0"),  # breaking, approved by label
        ("api-v1.9.9", False, True, "api-v1.10.0"),
    ],
)
def test_next_version(previous: str | None, breaking: bool, changed: bool, expected: str) -> None:
    assert next_version(previous, breaking=breaking, changed=changed) == expected


@pytest.mark.parametrize("previous", ["v1.0.0", "api-v1.0", "api-v1.0.0-rc1", "api-v01.0.0", ""])
def test_unexpected_previous_tags_are_refused(previous: str) -> None:
    with pytest.raises(ValueError, match="tag"):
        next_version(previous, breaking=False, changed=True)


@pytest.mark.parametrize(
    ("text", "count"),
    [("null", 0), ("[]", 0), ('[{"id": "endpoint-added"}]', 1), ("", 0)],
)
def test_changelog_entries(text: str, count: int) -> None:
    assert changelog_entries(text) == count


@pytest.mark.parametrize("text", ['{"id": 1}', "nope", "3"])
def test_unexpected_changelogs_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="changelog"):
        changelog_entries(text)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-m", "scripts.contract_version", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=ROOT,
    )


def _files(tmp_path: Path, changelog: object, codes: object) -> list[str]:
    changelog_file, codes_file = tmp_path / "changelog.json", tmp_path / "codes.json"
    changelog_file.write_text(json.dumps(changelog))
    codes_file.write_text(json.dumps(codes))
    return ["--changelog", str(changelog_file), "--codes", str(codes_file)]


NO_CODES: dict[str, list[str]] = {"removed": [], "added": []}


@pytest.mark.parametrize(
    ("previous", "breaking", "changelog", "codes", "expected"),
    [
        ("api-v1.0.0", "0", [{"id": "endpoint-added"}], NO_CODES, "api-v1.1.0"),
        ("", "0", [], NO_CODES, "api-v1.0.0"),
        ("api-v1.0.0", "0", [], NO_CODES, "api-v1.0.1"),
        # oasdiff cannot see error codes: scripts/contract_codes.py can
        ("api-v1.0.0", "0", [], {"removed": [], "added": ["ErrorCode CONFLICT"]}, "api-v1.1.0"),
        ("api-v1.0.0", "0", [], {"removed": ["ErrorCode X"], "added": []}, "api-v2.0.0"),
        ("api-v1.0.0", "1", [], NO_CODES, "api-v2.0.0"),
    ],
)
def test_the_command_prints_the_next_tag(
    tmp_path: Path, previous: str, breaking: str, changelog: object, codes: object, expected: str
) -> None:
    result = _run(
        "--previous", previous, "--breaking", breaking, *_files(tmp_path, changelog, codes)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"


@pytest.mark.parametrize(
    ("changelog", "codes", "message"),
    [({}, NO_CODES, "changelog"), ([], {"removed": "x"}, "codes"), ([], [], "codes")],
)
def test_the_command_refuses_bad_input(
    tmp_path: Path, changelog: object, codes: object, message: str
) -> None:
    result = _run(
        "--previous", "api-v1.0.0", "--breaking", "0", *_files(tmp_path, changelog, codes)
    )
    assert result.returncode == 2
    assert message in result.stderr
