"""GZ-062 and GZ-085: guards for the staging and production deploy config, which run
unattended against real databases. The files are read as text, like tests/test_ci_config.py:
a change that breaks a rule fails here instead of on Render.
"""

import base64
import hashlib
import re
from pathlib import Path

import pytest

from gize_api.core.config import database_ssl_mode, is_database_url
from tests.support import SettingsFactory

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = (ROOT / "render.yaml").read_text()
PRODUCTION_BLUEPRINT = (ROOT / "render.production.yaml").read_text()
BLUEPRINTS = {"staging": BLUEPRINT, "production": PRODUCTION_BLUEPRINT}
DEPLOY = (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text()
PRODUCTION_DEPLOY = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text()
CA_PATH = "docker/certs/supabase-root-2021-ca.crt"
CA_IN_IMAGE = "/app/certs/supabase-root-2021-ca.crt"
# Supabase Root 2021 CA (valid to April 2031), as downloaded from the project's database
# settings and checked against the eu-west-1 pooler's chain with verify-full (2026-11-02).
CA_SHA256 = "807025ad50d4ed219d2c9c7d299c004f824eb00cf7f65afef607d07b72e6cafa"
# Settings that hold credentials: never written into render.yaml.
SECRET_SETTINGS = {
    "DATABASE_URL",
    "MIGRATOR_DATABASE_URL",
    "BOOTSTRAP_ADMIN_DATABASE_URL",
    "REDIS_URL",
    "SUPABASE_SECRET_KEY",
    "VERIFIED_PHONE_TOKEN_KEY",
    "AFROMESSAGE_TOKEN",
    "SENTRY_DSN",
    "OTEL_EXPORTER_OTLP_HEADERS",
}


def _env_vars(blueprint: str = BLUEPRINT) -> dict[str, str]:
    """Each env var of a blueprint and the line after its key (its value or where it comes
    from). Staging's unless another is given."""
    return dict(re.findall(r"- key: ([A-Z_]+)[^\n]*\n\s+(.+)", blueprint))


def _keys(blueprint: str) -> list[str]:
    """Every env var key, in order, repeats included (`_env_vars` keeps only the last)."""
    return re.findall(r"- key: ([A-Z_]+)", blueprint)


# The Supabase CA


def test_the_ca_is_supabases_root_and_nothing_else() -> None:
    text = (ROOT / CA_PATH).read_text()
    blocks = re.findall(r"-----BEGIN ([A-Z ]+)-----\n(.+?)-----END \1-----", text, re.DOTALL)
    assert [kind for kind, _ in blocks] == ["CERTIFICATE"]  # one certificate, no key
    der = base64.b64decode("".join(blocks[0][1].split()))
    assert hashlib.sha256(der).hexdigest() == CA_SHA256


def test_the_image_carries_the_ca_where_staging_looks_for_it() -> None:
    # Plain COPY: owned by root and readable. `--chmod` on it once reached the directory on
    # Render's builder and left the app unable to open the file.
    assert f"\nCOPY {CA_PATH} {CA_IN_IMAGE}\n" in DOCKERFILE
    assert f"test -r {CA_IN_IMAGE} && ! test -w {CA_IN_IMAGE}" in CI  # CI reads it as uid 10001
    for blueprint in BLUEPRINTS.values():
        assert _env_vars(blueprint)["DATABASE_SSL_ROOT_CERT"] == f"value: {CA_IN_IMAGE}"
    for deploy in (DEPLOY, PRODUCTION_DEPLOY):
        assert f"DATABASE_SSL_ROOT_CERT: {CA_PATH}" in deploy  # migrations verify it too


# render.yaml


@pytest.mark.parametrize("environment", BLUEPRINTS)
def test_secrets_are_never_written_into_the_blueprint(environment: str) -> None:
    blueprint = BLUEPRINTS[environment]
    keys = _keys(blueprint)
    assert len(keys) == len(set(keys))  # one source per setting
    for key, source in _env_vars(blueprint).items():
        if key in SECRET_SETTINGS:
            assert source in ("sync: false", "fromService:"), key
    assert "sb_secret_" not in blueprint
    assert not re.search(r"(postgres(ql)?|rediss?)(\+\w+)?://", blueprint)
    assert not re.search(r"https://[a-z0-9]+@[^\s]*sentry", blueprint)  # a DSN holds its key


@pytest.mark.parametrize("environment", BLUEPRINTS)
def test_the_blueprint_runs_its_environment_and_never_deploys_on_its_own(
    environment: str,
) -> None:
    blueprint = BLUEPRINTS[environment]
    assert _env_vars(blueprint)["APP_ENV"] == f"value: {environment}"
    assert 'autoDeployTrigger: "off"' in blueprint  # the deploy workflow migrates first
    assert "healthCheckPath: /health/ready" in blueprint
    assert re.findall(r"region: (\w+)", blueprint) == ["frankfurt", "frankfurt"]
    assert re.findall(r"plan: (\w+)", blueprint) == ["free", "free"]  # owner, until ~2027-05
    names = re.findall(r"^            name: (\S+)$", blueprint, re.MULTILINE)
    assert names == [f"gize-api-{environment}", f"gize-redis-{environment}"]
    assert re.findall(r"^      - name: (\w+)$", blueprint, re.MULTILINE) == [environment]


@pytest.mark.parametrize("environment", BLUEPRINTS)
def test_render_sends_traffic_to_the_images_port(environment: str) -> None:
    port = re.search(r"--port (\d+)", ENTRYPOINT)
    assert port is not None
    assert _env_vars(BLUEPRINTS[environment])["PORT"] == f'value: "{port.group(1)}"'


def test_each_visitor_is_limited_by_the_address_cloudflare_saw() -> None:
    """GZ-072: every request to a Render web service passes through Cloudflare, which writes
    CF-Connecting-IP and refuses a request that sends its own (403, proved on staging). Without
    it, every visitor has the
    address of Render's proxy and all share one limit. X-Forwarded-For is only appended to
    (the caller writes its first entry), so uvicorn never reads it."""
    for blueprint in BLUEPRINTS.values():
        assert _env_vars(blueprint)["CLIENT_IP_HEADER"] == "value: CF-Connecting-IP"
    assert "--no-proxy-headers" in ENTRYPOINT
    assert "--forwarded-allow-ips" not in ENTRYPOINT


@pytest.mark.parametrize("environment", BLUEPRINTS)
def test_redis_is_private_and_keeps_what_it_holds(environment: str) -> None:
    blueprint = BLUEPRINTS[environment]
    assert "ipAllowList: []" in blueprint
    assert "maxmemoryPolicy: noeviction" in blueprint
    assert _env_vars(blueprint)["REDIS_URL"] == "fromService:"
    source = re.search(
        r"- key: REDIS_URL\n\s+fromService:\n\s+type: keyvalue\n\s+name: (\S+)\n", blueprint
    )
    assert source is not None
    assert source.group(1) == f"gize-redis-{environment}"  # its own, never the other's


def test_staging_takes_both_portals_in_a_shape_the_settings_accept(
    make_settings: SettingsFactory,
) -> None:
    """Owner, 2026-11-22: the staging portal (staging-admin) can call staging before
    production exists. admin.gizetransport.com comes off staging when production goes live
    (GZ-085). A malformed list (no https, a path, a trailing slash) fails here, not on Render."""
    source = _env_vars()["CORS_ALLOWED_ORIGINS"]
    assert source.startswith("value: ")
    settings = make_settings(app_env="staging", cors_allowed_origins=source.removeprefix("value: "))
    assert settings.cors_allowed_origins == (
        "https://admin.gizetransport.com",
        "https://staging-admin.gizetransport.com",
    )


def test_production_takes_only_the_production_portal(make_settings: SettingsFactory) -> None:
    """Owner, 2026-11-20: admin.gizetransport.com talks only to the production API."""
    source = _env_vars(PRODUCTION_BLUEPRINT)["CORS_ALLOWED_ORIGINS"]
    assert source.startswith("value: ")
    settings = make_settings(
        app_env="production",
        cors_allowed_origins=source.removeprefix("value: "),
        observability_required=False,
    )
    assert settings.cors_allowed_origins == ("https://admin.gizetransport.com",)


def test_production_asks_for_its_own_observability_and_no_sms_secrets() -> None:
    """Production refuses to start without Sentry and OTLP (owner, 2026-11-23: a free Sentry
    account). SMS is paused (2026-11-19): its secrets are added when the owner lifts it."""
    env = _env_vars(PRODUCTION_BLUEPRINT)
    for key in ("SENTRY_DSN", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"):
        assert env[key] == "sync: false", key
    for key in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "DATABASE_URL"):
        assert env[key] == "sync: false", key  # its own project, never staging's
    assert "pahktmbnbyulqkhdruci" not in PRODUCTION_BLUEPRINT
    assert not {"AFROMESSAGE_TOKEN", "VERIFIED_PHONE_TOKEN_KEY"} & set(env)
    assert "OBSERVABILITY_REQUIRED" not in env


# The staging deploy workflow


def test_it_deploys_only_main_after_ci_passed_on_it() -> None:
    ci_name = re.search(r"^name: (.+)$", CI, re.MULTILINE)
    assert ci_name is not None
    assert f"workflows: [{ci_name.group(1)}]" in DEPLOY
    assert "types: [completed]" in DEPLOY
    for condition in (
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
    ):
        assert condition in DEPLOY
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in DEPLOY


def test_it_deploys_only_mains_current_tip() -> None:
    """CI runs finish in any order and can be re-run: an older commit must not replace a
    newer one. Every step after the check depends on it."""
    steps = DEPLOY.split("\n    steps:\n", 1)[1]
    first, *later = re.split(r"\n      - ", "\n" + steps)[1:]
    assert first.startswith("name: Check this commit is still main's tip")
    assert 'repos/${GITHUB_REPOSITORY}/commits/main" --jq .sha' in first
    assert later
    for step in later:
        assert "if: steps.tip.outputs.current == 'true'" in step, step.splitlines()[0]
    repository = "github.event.workflow_run.head_repository.full_name == github.repository"
    assert repository in DEPLOY


def test_it_migrates_before_it_deploys_one_at_a_time() -> None:
    migrate = DEPLOY.index("alembic upgrade head")
    deploy = DEPLOY.index("DEPLOY_HOOK_URL}&ref=${COMMIT}")
    assert migrate < deploy
    assert "group: deploy-staging" in DEPLOY
    assert "cancel-in-progress: false" in DEPLOY  # never stop a migration halfway


def test_it_holds_no_more_power_than_it_needs() -> None:
    assert re.findall(r"^\s*permissions:\n\s+(.+)$", DEPLOY, re.MULTILINE) == ["contents: read"]
    assert "persist-credentials: false" in DEPLOY
    for line in DEPLOY.splitlines():
        if "secrets." in line and not line.lstrip().startswith("#"):
            # Only into a step's environment: never into a command line or a log.
            assert re.fullmatch(r"\s+[A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}", line), line


def test_actions_and_tools_match_ci() -> None:
    uses = re.findall(r"uses: (\S+)", DEPLOY)
    assert uses
    assert all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action) for action in uses)
    assert set(uses) <= set(re.findall(r"uses: (\S+)", CI))
    for name in ("UV_PYTHON", "UV_VERSION"):
        pattern = rf'^  {name}: "([^"]+)"$'
        assert re.findall(pattern, DEPLOY, re.MULTILINE) == re.findall(pattern, CI, re.MULTILINE), (
            name
        )


# The production deploy workflow (GZ-085)

STAGING_DEPLOY_STEP = "Deploy this commit (Render deploy hook)"


def _steps(workflow: str) -> list[str]:
    steps = workflow.split("\n    steps:\n", 1)[1]
    return re.split(r"\n      - ", "\n" + steps)[1:]


def test_production_deploys_only_when_the_owner_runs_and_approves_it() -> None:
    """Never on its own: the Run button on main, then the `production` environment's
    required reviewer. Only that environment holds production's secrets."""
    triggers = PRODUCTION_DEPLOY.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert triggers.strip() == "workflow_dispatch:"
    assert "if: github.ref == 'refs/heads/main'" in PRODUCTION_DEPLOY
    assert re.findall(r"^    environment: (\S+)", PRODUCTION_DEPLOY, re.MULTILINE) == ["production"]
    assert "ref: ${{ github.sha }}" in PRODUCTION_DEPLOY  # the commit it checked


def test_production_deploys_only_what_staging_already_deployed() -> None:
    """First step: a successful Deploy staging run deployed this very commit. Every step
    after it depends on it."""
    assert f"- name: {STAGING_DEPLOY_STEP}\n" in DEPLOY  # the step it looks for exists
    first, *later = _steps(PRODUCTION_DEPLOY)
    assert first.startswith("name: Check staging already deployed this commit")
    assert "workflows/deploy-staging.yml/runs?head_sha=${COMMIT}&status=success" in first
    assert STAGING_DEPLOY_STEP in first
    # The deploy step itself succeeded: not a run that skipped it (no longer main's tip).
    assert 'select(.name == env.STEP and .conclusion == \\"success\\")' in first
    assert 'repos/${GITHUB_REPOSITORY}/commits/main" --jq .sha' in first  # still main's tip
    assert "exit 1" in first
    assert later
    for step in later:
        assert "if: steps.staged.outputs.deployed == 'true'" in step, step.splitlines()[0]


def test_production_migrates_before_it_deploys_one_at_a_time() -> None:
    migrate = PRODUCTION_DEPLOY.index("alembic upgrade head")
    deploy = PRODUCTION_DEPLOY.index("DEPLOY_HOOK_URL}&ref=${COMMIT}")
    assert migrate < deploy
    assert "APP_ENV: production  #" in PRODUCTION_DEPLOY
    assert "group: deploy-production" in PRODUCTION_DEPLOY
    assert "cancel-in-progress: false" in PRODUCTION_DEPLOY


def test_production_holds_no_more_power_than_it_needs() -> None:
    permissions = re.search(r"^permissions:\n((?:  .+\n)+)", PRODUCTION_DEPLOY, re.MULTILINE)
    assert permissions is not None
    granted = [line.split("#")[0].strip() for line in permissions.group(1).splitlines()]
    assert granted == ["contents: read", "actions: read"]
    assert PRODUCTION_DEPLOY.count("permissions:") == 1
    assert "persist-credentials: false" in PRODUCTION_DEPLOY
    secrets = set(re.findall(r"secrets\.([A-Z_]+)", PRODUCTION_DEPLOY))
    assert secrets == {"PRODUCTION_MIGRATOR_DATABASE_URL", "RENDER_PRODUCTION_DEPLOY_HOOK_URL"}
    for line in PRODUCTION_DEPLOY.splitlines():
        if "secrets." in line and not line.lstrip().startswith("#"):
            assert re.fullmatch(r"\s+[A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}", line), line
    assert "STAGING" not in PRODUCTION_DEPLOY  # never staging's secrets


def test_production_actions_and_tools_match_ci() -> None:
    uses = re.findall(r"uses: (\S+)", PRODUCTION_DEPLOY)
    assert uses == re.findall(r"uses: (\S+)", DEPLOY)
    for name in ("UV_PYTHON", "UV_VERSION"):
        pattern = rf'^  {name}: "([^"]+)"$'
        assert re.findall(pattern, PRODUCTION_DEPLOY, re.MULTILINE) == re.findall(
            pattern, CI, re.MULTILINE
        ), name


# The local staging and production files


@pytest.mark.parametrize("environment", BLUEPRINTS)
def test_the_template_urls_are_ones_the_settings_accept(environment: str) -> None:
    template = (ROOT / f".env.{environment}.example").read_text()
    assert f"\nAPP_ENV={environment}\n" in template
    urls = dict(re.findall(r"^([A-Z_]+_URL)=(postgresql://\S+)$", template, re.MULTILINE))
    assert set(urls) == {"BOOTSTRAP_ADMIN_DATABASE_URL", "DATABASE_URL", "MIGRATOR_DATABASE_URL"}
    for name, url in urls.items():
        filled = re.sub(r"<[a-z-]+>", "Placeholder1", url)
        assert is_database_url(filled), name
        assert database_ssl_mode(filled) == "verify-full", name
    assert f"DATABASE_SSL_ROOT_CERT={CA_PATH}" in template


def test_the_production_template_never_names_staging() -> None:
    template = (ROOT / ".env.production.example").read_text()
    assert "pahktmbnbyulqkhdruci" not in template
    # The one-off commands run from a laptop with no Sentry or OTLP: production's settings
    # would refuse them otherwise. The deployed API never reads this file.
    assert "\nOBSERVABILITY_REQUIRED=false\n" in template


def test_the_filled_in_files_are_never_committed() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env.*" in ignored
    assert [line for line in ignored if line.startswith("!.env")] == [
        "!.env.example",
        "!.env.staging.example",
        "!.env.production.example",
    ]
