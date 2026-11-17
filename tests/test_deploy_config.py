"""GZ-062: guards for the staging deploy config, which runs unattended against a real
database. The files are read as text, like tests/test_ci_config.py: a change that breaks a
rule fails here instead of on Render.
"""

import base64
import hashlib
import re
from pathlib import Path

from gize_api.core.config import database_ssl_mode, is_database_url

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = (ROOT / "render.yaml").read_text()
DEPLOY = (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text()
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


def _env_vars() -> dict[str, str]:
    """Each render.yaml env var and the line after its key (its value or where it comes from)."""
    return dict(re.findall(r"- key: ([A-Z_]+)[^\n]*\n\s+(.+)", BLUEPRINT))


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
    assert _env_vars()["DATABASE_SSL_ROOT_CERT"] == f"value: {CA_IN_IMAGE}"
    assert f"DATABASE_SSL_ROOT_CERT: {CA_PATH}" in DEPLOY  # migrations verify it too


# render.yaml


def test_secrets_are_never_written_into_the_blueprint() -> None:
    for key, source in _env_vars().items():
        if key in SECRET_SETTINGS:
            assert source in ("sync: false", "fromService:"), key
    assert "sb_secret_" not in BLUEPRINT
    assert not re.search(r"(postgres(ql)?|rediss?)(\+\w+)?://", BLUEPRINT)


def test_the_blueprint_runs_staging_and_never_deploys_on_its_own() -> None:
    env = _env_vars()
    assert env["APP_ENV"] == "value: staging"
    assert 'autoDeployTrigger: "off"' in BLUEPRINT  # the deploy workflow migrates first
    assert "healthCheckPath: /health/ready" in BLUEPRINT
    assert re.findall(r"region: (\w+)", BLUEPRINT) == ["frankfurt", "frankfurt"]


def test_render_sends_traffic_to_the_images_port() -> None:
    port = re.search(r"--port (\d+)", ENTRYPOINT)
    assert port is not None
    assert _env_vars()["PORT"] == f'value: "{port.group(1)}"'


def test_each_visitor_is_limited_by_the_address_cloudflare_saw() -> None:
    """GZ-072: every request to a Render web service passes through Cloudflare, which writes
    CF-Connecting-IP and refuses a request that sends its own (403, proved on staging). Without
    it, every visitor has the
    address of Render's proxy and all share one limit. X-Forwarded-For is only appended to
    (the caller writes its first entry), so uvicorn never reads it."""
    assert _env_vars()["CLIENT_IP_HEADER"] == "value: CF-Connecting-IP"
    assert "--no-proxy-headers" in ENTRYPOINT
    assert "--forwarded-allow-ips" not in ENTRYPOINT


def test_redis_is_private_and_keeps_what_it_holds() -> None:
    assert "ipAllowList: []" in BLUEPRINT
    assert "maxmemoryPolicy: noeviction" in BLUEPRINT
    assert _env_vars()["REDIS_URL"] == "fromService:"


# The deploy workflow


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


# The local staging file


def test_the_staging_template_urls_are_ones_the_settings_accept() -> None:
    template = (ROOT / ".env.staging.example").read_text()
    urls = dict(re.findall(r"^([A-Z_]+_URL)=(postgresql://\S+)$", template, re.MULTILINE))
    assert set(urls) == {"BOOTSTRAP_ADMIN_DATABASE_URL", "DATABASE_URL", "MIGRATOR_DATABASE_URL"}
    for name, url in urls.items():
        filled = re.sub(r"<[a-z-]+>", "Placeholder1", url)
        assert is_database_url(filled), name
        assert database_ssl_mode(filled) == "verify-full", name
    assert f"DATABASE_SSL_ROOT_CERT={CA_PATH}" in template


def test_the_filled_in_staging_file_is_never_committed() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env.*" in ignored
    assert [line for line in ignored if line.startswith("!.env")] == [
        "!.env.example",
        "!.env.staging.example",
    ]
