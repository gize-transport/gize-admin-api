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
cp .env.example .env                  # optional: override local ports/password
uv sync                               # create .venv from uv.lock
docker compose up -d --wait           # Postgres 17 + Redis 7.4 on 127.0.0.1
uv run pytest                         # tests + coverage gate
```

## Everyday commands

| Task | Command |
| --- | --- |
| Install / update the environment | `uv sync` |
| Start / stop local services | `docker compose up -d --wait` / `docker compose down` |
| Run the API locally | `uv run fastapi dev src/gize_api/main.py` |
| Tests with coverage (85% overall) | `uv run pytest` |
| Services coverage gate (90%), after tests | `uv run python -m scripts.check_service_coverage` |
| Lint, format check, types | `uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |
| Dependency audit | `uv export --frozen --all-groups --no-emit-project --format requirements-txt \| uv run pip-audit --strict --disable-pip --require-hashes -r /dev/stdin` |

Commands for migrations, the background worker and the OpenAPI export are added by the tickets that introduce them (GZ-006, GZ-008, GZ-015).

## Docker image

One image serves both processes:

```bash
docker build -t gize-admin-api .
docker run --rm -p 8000:8000 gize-admin-api          # api (default)
docker run --rm gize-admin-api worker                # Arq worker (from GZ-008)
```

The container runs as an unprivileged user (uid 10001). The API finishes in-flight requests for up to 30 s after SIGTERM, so give the container a stop timeout of at least 35 s on whichever host runs it (Docker's default is 10 s: `docker stop -t 35`, or `stop_grace_period: 35s` in Compose).

## Working on a ticket

Follow [`docs/workflow.md`](docs/workflow.md): one ticket, one branch (`feat/GZ-xxx-short-name`), one PR titled `GZ-xxx: <title>` using the PR template. Never push to `main` directly.
