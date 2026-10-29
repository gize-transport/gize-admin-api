"""Guards for the CI configuration, which has no branch protection behind it.

The merge rule is "every check green", but `gh pr checks` only lists checks that ran, so a
deleted job would look green. These tests pin the job list and keep versions that are
written in more than one place from drifting apart.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "contract-release.yml").read_text()

EXPECTED_CHECKS = {
    "lint": "Lint, types, dependency audit",
    "secrets": "Secret scan (gitleaks)",
    "test": "Tests and coverage gates",
    "migrations": "Migrations (upgrade, downgrade, upgrade)",
    "docker": "Docker image build",
    "contract": "API contract (up to date, no breaking changes)",
}


def _one(pattern: str, text: str) -> str:
    matches = re.findall(pattern, text, flags=re.MULTILINE)
    assert len(matches) == 1, f"expected exactly one match for {pattern!r}, got {matches}"
    return str(matches[0])


def _jobs() -> dict[str, str]:
    jobs_block = CI.split("\njobs:\n", 1)[1]
    return dict(re.findall(r"^  ([a-z][a-z0-9_-]*):\n    name: (.+)$", jobs_block, re.MULTILINE))


def test_ci_has_exactly_the_expected_checks() -> None:
    assert _jobs() == EXPECTED_CHECKS


def test_readme_lists_every_check_name() -> None:
    readme = (ROOT / "README.md").read_text()
    for name in EXPECTED_CHECKS.values():
        assert f"| {name} |" in readme


def test_uv_version_matches_dockerfile_and_pyproject() -> None:
    ci_uv = _one(r'^  UV_VERSION: "([\d.]+)"$', CI)
    dockerfile = (ROOT / "Dockerfile").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert _one(r"^ARG UV_IMAGE=ghcr\.io/astral-sh/uv:([\d.]+)@", dockerfile) == ci_uv
    assert _one(r'^required-version = ">=([\d.]+),', pyproject) == ci_uv


def test_gitleaks_version_matches_pre_commit_hook() -> None:
    ci_gitleaks = _one(r'^  GITLEAKS_VERSION: "([\d.]+)"$', CI)
    pre_commit = (ROOT / ".pre-commit-config.yaml").read_text()
    hook = _one(
        r"repo: https://github\.com/gitleaks/gitleaks\n\s+rev: \w+  # frozen: v([\d.]+)$",
        pre_commit,
    )
    assert hook == ci_gitleaks


def test_ci_service_images_match_docker_compose() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for image in ("postgres", "redis"):
        pattern = rf"image: ({image}:[^\s@]+@sha256:[0-9a-f]{{64}})"
        in_compose = set(re.findall(pattern, compose))
        assert len(in_compose) == 1, in_compose  # the dev and test servers match too
        assert set(re.findall(pattern, CI)) == in_compose


def test_ci_python_matches_docker_image() -> None:
    ci_python = _one(r'^  UV_PYTHON: "(3\.12\.\d+)"$', CI)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert _one(r"^ARG PYTHON_IMAGE=python:([\d.]+)-slim", dockerfile) == ci_python


def test_oasdiff_is_pinned_the_same_in_both_workflows() -> None:
    for name in ("OASDIFF_VERSION", "OASDIFF_SHA256"):
        pattern = rf'^  {name}: "([0-9a-f.]+)"$'
        assert _one(pattern, CI) == _one(pattern, RELEASE)


def test_the_breaking_change_gate_reruns_when_labels_change() -> None:
    """The `breaking-change-approved` label is read from the event: CI must rerun on it."""
    types = _one(r"^  pull_request:\n    types: \[([^\]]+)\]", CI)
    assert {"labeled", "unlabeled", "opened", "synchronize", "reopened"} <= {
        part.strip() for part in types.split(",")
    }


def test_only_the_release_job_may_write() -> None:
    assert "contents: write" not in CI
    assert _one(r"^permissions:\n  contents: (\w+)$", RELEASE) == "read"
    assert RELEASE.count("contents: write") == 1
    release_job = RELEASE.split("\n  release:\n", 1)[1]
    assert "contents: write" in release_job
    assert "uv " not in release_job  # the job with the write token runs no project code


def test_both_workflows_use_the_severity_levels() -> None:
    """New response enum values (error codes, roles) are additive per docs/api-contract.md;
    oasdiff 1.33 calls them breaking. No comments allowed: oasdiff reads every line."""
    levels = (ROOT / ".github" / "oasdiff-levels.txt").read_text()
    assert levels == "response-property-enum-value-added info\n"
    for workflow in (CI, RELEASE):
        calls = [
            line
            for line in workflow.splitlines()
            if 'oasdiff" breaking' in line or 'oasdiff" changelog' in line
        ]
        assert calls
        for line in calls:
            assert "--severity-levels" in line, line
