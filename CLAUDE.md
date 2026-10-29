# Gize Admin API (backend)

FastAPI backend for the Gize Transport admin portal and the rider registration app. It owns **all business rules and every write**. Supabase provides Postgres, Auth and Storage only. The admin frontend (Next.js) lives in a separate repo and talks to this API over HTTP.

This backend is critical. Write code you would be comfortable running unattended at 6:30 a.m. with real riders waiting. When in doubt, choose the boring, explicit, well-tested option and ask.

## Read before any work

1. `docs/product-brief.md` – what Gize is, who uses this API, business rules
2. `docs/architecture.md` – components, layers, folder layout, data model, conventions
3. `docs/engineering-standards.md` – security, database, reliability, rate limiting, observability, testing. **Non-negotiable.**
4. `docs/workflow.md` – how every ticket goes from plan to merged PR
5. `docs/milestones.md` – the tickets. Every branch and PR maps to exactly one ticket ID (e.g. `GZ-012`).
6. `docs/api-contract.md` – the contract with the admin frontend repo (`gize-admin-web`). This repo owns it.

If these documents conflict, `engineering-standards.md` and `api-contract.md` win, then `architecture.md`, then the rest. If a ticket seems to require breaking a standard, stop and ask.

## Stack (decided – do not change without asking)

- Python 3.12, FastAPI, Pydantic v2 (+ pydantic-settings), Uvicorn
- SQLAlchemy 2.0 (async) + asyncpg; Alembic for every schema change
- Supabase: Postgres (schema `gize`), Auth (JWT verification only), Storage (driver documents)
- Redis: rate limiting (`limits`, async; replaced slowapi, which blocks on Redis: owner decision at GZ-009), Arq background jobs
- Observability: structlog JSON logs, OpenTelemetry (metrics + traces → Grafana Cloud), Sentry (errors)
- Tooling: uv, ruff (lint + format), mypy `--strict`, pytest, pytest-asyncio, coverage, pre-commit, gitleaks, pip-audit
- Packaging: Docker image; hosting provider not chosen yet, so nothing may depend on a specific host
- CI: GitHub Actions

Ask before adding any dependency not listed here.

## Non-negotiables (summary – details in engineering-standards.md)

- Every handler checks **role and object ownership**; "logged in" is never enough.
- Pydantic schemas validate all input and **forbid unknown fields**; size limits on everything.
- No raw SQL strings, no string-built queries, no shell commands from input.
- Errors to clients: generic message + error code + request ID. Never stack traces, SQL or internal IDs.
- Secrets only from settings/env; never logged, returned or committed.
- The app connects as `gize_app` (no DDL). Migrations run as `gize_migrator`.
- Every list is paginated and bounded. Every network call has a timeout.
- Migrations are expand/contract with a tested downgrade.
- Logs never contain names, children's data, tokens or unmasked phone numbers.
- A ticket is **done** only when its tests cover: happy path, unauthenticated, wrong role/owner, invalid input, dependency failure – against a real Postgres, never a mocked database.

## Commands

- `uv sync` – install
- `docker compose up -d` – local Postgres + Redis
- `uv run alembic upgrade head` – apply migrations
- `uv run --env-file .env uvicorn --factory gize_api.main:create_app --reload` – run locally
- `uv run pytest` – tests (must pass, coverage gate in `pyproject.toml`)
- `uv run ruff check . && uv run ruff format --check . && uv run mypy src` – must pass
- `uv run --env-file .env python -m gize_api.worker` – background worker (not Arq's CLI: it replaces our JSON logging)
- `uv run python scripts/export_openapi.py` – regenerate `openapi.json` (commit it with any API change)
- `uv run --env-file .env python -m gize_api.cli.first_admin --user-id <uid> --name "…"` – make the first admin (README, "First admin")

## Working style

Follow `docs/workflow.md` exactly: plan → wait for approval → tests first → code → self-review checklist → PR. One ticket per branch and PR. Never commit directly to `main`. Never skip, weaken or delete a failing test to make CI pass; fix the cause or ask.

### Merging without branch protection (free GitHub plan)

GitHub does not enforce protection on our private repos, so these rules are the protection. Follow them exactly:
- Never push or commit directly to `main`. The `.githooks/pre-push` hook blocks it; never bypass it (`--no-verify` is forbidden).
- Merge a PR only after **every** CI check has passed: run `gh pr checks <number> --watch`; if all checks are green, run `gh pr merge <number> --squash --delete-branch`. If any check fails or is pending, do not merge – fix the cause on the branch.
- Never merge with failing, skipped or cancelled checks, and never use admin override flags.
