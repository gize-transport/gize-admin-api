"""Guards for the CI configuration, which has no branch protection behind it.

The merge rule is "every check green", but `gh pr checks` only lists checks that ran, so a
deleted job would look green. These tests pin the job list and keep versions that are
written in more than one place from drifting apart.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "contract-release.yml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DEPENDABOT = (ROOT / ".github" / "dependabot.yml").read_text()
PIN_CHECK = (ROOT / ".github" / "workflows" / "pin-check.yml").read_text()
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

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
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert _one(r"^FROM ghcr\.io/astral-sh/uv:([\d.]+)@sha256:[0-9a-f]{64} AS uv$", DOCKERFILE) == (
        ci_uv
    )
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
    stages = re.findall(r"^FROM python:([\d.]+)-slim-trixie@sha256:([0-9a-f]{64}) AS (\w+)$",
                        DOCKERFILE, re.MULTILINE)  # fmt: skip
    # The build and the image it fills: one Python, so Dependabot updates both together.
    assert [stage for *_, stage in stages] == ["builder", "runtime"]
    assert {(version, digest) for version, digest, _ in stages} == {(ci_python, stages[0][1])}


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


def test_release_notes_come_from_the_notes_script() -> None:
    """oasdiff's own text changelog grew past what a step can be given (GZ-021's release)."""
    version_job = RELEASE.split("\n  release:\n", 1)[0]
    assert "python -m scripts.contract_notes" in version_job
    assert 'oasdiff" changelog --severity-levels' not in version_job  # the long text form


# GZ-065: something proposes every update; nothing merges one but a person.


def test_every_image_is_a_literal_digest_pinned_from_line() -> None:
    """Dependabot reads only literal FROM lines: an ARG-built one would never be updated."""
    froms = re.findall(r"^FROM (.+)$", DOCKERFILE, re.MULTILINE)
    assert len(froms) == 3
    for line in froms:
        assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64} AS \w+", line), line
    assert "ARG " not in DOCKERFILE.split("\nFROM ", 1)[0]


def test_every_action_is_pinned_to_a_commit() -> None:
    for workflow in WORKFLOWS:
        for action in re.findall(r"uses: (\S+)", workflow.read_text()):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), f"{workflow.name}: {action}"


def _updates() -> dict[str, str]:
    """Each update block of dependabot.yml, by ecosystem."""
    blocks = re.split(r"^  - (?=package-ecosystem:)", DEPENDABOT, flags=re.MULTILINE)[1:]
    return {_one(r'^package-ecosystem: "([\w-]+)"$', block): block for block in blocks}


def test_dependabot_watches_every_kind_of_pin() -> None:
    assert set(_updates()) == {"uv", "github-actions", "docker", "docker-compose", "pre-commit"}


@pytest.mark.parametrize(
    "ecosystem", ["uv", "github-actions", "docker", "docker-compose", "pre-commit"]
)
def test_each_ecosystem_is_weekly_grouped_and_cooled_down(ecosystem: str) -> None:
    block = _updates()[ecosystem]
    assert re.search(r'^    directory: "/"$', block, re.MULTILINE)
    assert re.search(r'^      interval: "weekly"$', block, re.MULTILINE)
    # Minor and patch together, one PR; a major alone, to be read on its own.
    assert re.search(r'update-types: \["minor", "patch"\]', block)
    # A release a few days old: time for a bad or hijacked one to be pulled.
    assert re.search(r"^    cooldown:\n      default-days: [3-9]$", block, re.MULTILINE)
    assert re.search(r'^      prefix: "deps"$', block, re.MULTILINE)


@pytest.mark.parametrize(
    ("ecosystem", "dependency", "versions"),
    [
        ("docker", "python", ">=3.13"),  # pyproject: >=3.12,<3.13
        ("docker", "ghcr.io/astral-sh/uv", ">=0.12"),  # pyproject: required-version <0.12
        ("docker-compose", "postgres", ">=18"),  # Supabase runs 17
        ("docker-compose", "redis", ">=8"),  # Render's Key Value runs 7.4
    ],
)
def test_versions_we_cannot_take_are_ignored(
    ecosystem: str, dependency: str, versions: str
) -> None:
    rule = rf'- dependency-name: "{re.escape(dependency)}"\n\s+versions: \["{versions}"\]'
    assert re.search(rule, _updates()[ecosystem])


def test_the_runbook_says_how_to_finish_a_python_or_uv_bump() -> None:
    """Python and uv are pinned in the image and the workflows: an image bump fails CI until
    the workflows follow (RUNBOOK, "Dependency updates"), never drifts quietly."""
    runbook = (ROOT / "RUNBOOK.md").read_text()
    assert "## Dependency updates" in runbook
    for name in ("UV_PYTHON", "UV_VERSION", "required-version"):
        assert name in runbook.split("## Dependency updates", 1)[1]


def test_nothing_merges_on_its_own() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        assert "gh pr merge" not in text, workflow.name
        assert "--auto" not in text, workflow.name
        assert "enable-pull-request-automerge" not in text, workflow.name


def test_the_pin_check_runs_weekly_and_on_demand() -> None:
    assert re.search(
        r'^  schedule:\n    - cron: "\d+ \d+ \* \* 1"(  #.*)?$', PIN_CHECK, re.MULTILINE
    )
    assert re.search(r"^  workflow_dispatch:", PIN_CHECK, re.MULTILINE)


def test_the_pin_check_writes_issues_only_from_a_job_running_no_project_code() -> None:
    assert _one(r"^permissions:\n  contents: (\w+)$", PIN_CHECK) == "read"
    assert "contents: write" not in PIN_CHECK
    assert PIN_CHECK.count("issues: write") == 1
    report_job, issue_job = PIN_CHECK.split("\n  issue:\n", 1)
    assert "issues: write" in issue_job
    assert "scripts." not in issue_job
    assert "uv " not in issue_job
    assert "python -m scripts.stale_pins" in report_job


def test_every_workflow_uses_ci_s_python_and_actions() -> None:
    """A Dependabot PR updates every workflow at once; these keep a hand edit honest too."""
    ci_python = _one(r'^  UV_PYTHON: "([\d.]+)"$', CI)
    ci_actions = set(re.findall(r"uses: (\S+)", CI))
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        assert set(re.findall(r'^  UV_PYTHON: "([\d.]+)"$', text, re.MULTILINE)) <= {ci_python}
        assert set(re.findall(r"uses: (\S+)", text)) <= ci_actions, workflow.name
