# Gize Admin API

FastAPI backend for the Gize Transport admin portal and the rider registration app. It owns every business rule and every write; Supabase provides Postgres, Auth and Storage only.

Start with [`CLAUDE.md`](CLAUDE.md) and the documents in [`docs/`](docs/). [`docs/engineering-standards.md`](docs/engineering-standards.md) is non-negotiable.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.11.30 or newer within 0.11 (it installs Python 3.12 for you)
- Docker with Compose v2
- GitHub CLI (`gh auth login`) to open pull requests

## First-time setup

```bash
git config core.hooksPath .githooks   # pre-commit checks + blocks direct pushes to main
cp .env.example .env                  # local settings (placeholders only)
uv sync                               # create .venv from uv.lock
docker compose up -d --wait           # Postgres 17 + Redis 7.4 on 127.0.0.1
uv run pytest                         # tests + coverage gate
```

## Everyday commands

| Task | Command |
| --- | --- |
| Install / update the environment | `uv sync` |
| Start / stop local services | `docker compose up -d --wait` / `docker compose down` |
| Run the API locally | `uv run --env-file .env uvicorn --factory gize_api.main:create_app --reload` |
| Tests with coverage (85% overall) | `uv run pytest` |
| Services coverage gate (90%), after tests | `uv run python -m scripts.check_service_coverage` |
| Lint, format check, types | `uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |
| Coverage must not drop, after tests | `uv run python -m scripts.check_coverage_baseline` (`--update` raises the baseline) |
| Dependency audit | `uv export --frozen --all-groups --no-emit-project --format requirements-txt \| uv run pip-audit --strict --disable-pip --require-hashes -r /dev/stdin` |

Commands for migrations, the background worker and the OpenAPI export are added by the tickets that introduce them (GZ-006, GZ-008, GZ-015).

## Configuration

All configuration comes from environment variables (`src/gize_api/core/config.py`); the API refuses to start when a required one is missing or invalid, and the error names the variable, never its value. `.env.example` lists every variable with a placeholder.

| Variable | Required | Notes |
| --- | --- | --- |
| `APP_ENV` | yes | `local`, `test`, `staging` or `production`. Docs (`/docs`, `/redoc`, `/openapi.json`) are served only in `local` and `staging`. |
| `REDIS_URL` | yes, secret | `redis://` or `rediss://` URL. |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated origins (`scheme://host[:port]`, lowercase, no path); `https` only in `staging` and `production`. Empty: no cross-origin access. |
| `LOG_LEVEL` | no | `INFO` by default. |

## Docker image

One image serves both processes:

```bash
docker build -t gize-admin-api .
docker run --rm -p 8000:8000 -e APP_ENV=local \
  -e REDIS_URL=redis://host.docker.internal:6379/0 gize-admin-api   # api (default)
docker run --rm gize-admin-api worker                # Arq worker (from GZ-008)
```

The container runs as an unprivileged user (uid 10001). The API finishes in-flight requests for up to 30 s after SIGTERM, so give the container a stop timeout of at least 35 s on whichever host runs it (Docker's default is 10 s: `docker stop -t 35`, or `stop_grace_period: 35s` in Compose).

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

| Check | What fails it |
| --- | --- |
| Lint, types, dependency audit | `uv.lock` out of date; any pre-commit hook (ruff, ruff format, mypy `--strict`, file hygiene, TODO without a ticket); `pip-audit` finds a known vulnerability in any locked dependency |
| Secret scan (gitleaks) | gitleaks finds a secret in any commit reachable from the PR |
| Tests and coverage gates | a failing test; total coverage < 85%; service modules < 90%; total coverage differs from `.coverage-baseline` |
| Docker image build | the image does not build, does not run as uid 10001, or cannot import the app |

There is no branch protection on our plan, so CLAUDE.md's merge rules are the protection. Before merging, confirm **all four checks above are listed and green** on the PR: `gh pr checks` only shows checks that ran, so a job deleted from the workflow would not show up as failing. `tests/test_ci_config.py` pins the job list, and any change under `.github/workflows/` needs an explicit reviewer look.

**Coverage ratchet.** `.coverage-baseline` holds `main`'s total coverage. CI fails when coverage drops and also when it rises without the baseline being raised, so a later PR cannot quietly give the gain back. When coverage improves, run `uv run python -m scripts.check_coverage_baseline --update` and commit the file. Lowering it is a hand edit the PR must justify.

**If gitleaks finds a real secret** that already reached `main`: rotate the secret first (it is compromised whatever happens to the history), then add the finding's fingerprint (printed by gitleaks) to `.gitleaksignore` with a comment naming the rotation, in its own PR.

## Working on a ticket

Follow [`docs/workflow.md`](docs/workflow.md): one ticket, one branch (`feat/GZ-xxx-short-name`), one PR titled `GZ-xxx: <title>` using the PR template. Never push to `main` directly.
