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
docker compose up -d --wait           # Postgres 17 (dev + test servers) + Redis 7.4 on 127.0.0.1
uv run --env-file .env python -m gize_api.core.bootstrap --create-supabase-roles
uv run --env-file .env alembic upgrade head
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
| Apply / undo migrations | `uv run --env-file .env alembic upgrade head` / `… alembic downgrade -1` |
| New migration | `uv run --env-file .env alembic revision --autogenerate --rev-id 0002 -m "short name"` |
| Dependency audit | `uv export --frozen --all-groups --no-emit-project --format requirements-txt \| uv run pip-audit --strict --disable-pip --require-hashes -r /dev/stdin` |

Commands for the background worker and the OpenAPI export are added by the tickets that introduce them (GZ-008, GZ-015).

## Configuration

All configuration comes from environment variables (`src/gize_api/core/config.py`); the API refuses to start when a required one is missing or invalid, and the error names the variable, never its value. `.env.example` lists every variable with a placeholder.

| Variable | Required | Notes |
| --- | --- | --- |
| `APP_ENV` | yes | `local`, `test`, `staging` or `production`. Docs (`/docs`, `/redoc`, `/openapi.json`) are served only in `local` and `staging`. |
| `REDIS_URL` | yes, secret | `redis://` or `rediss://` URL. |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated origins (`scheme://host[:port]`, lowercase, no path); `https` only in `staging` and `production`. Empty: no cross-origin access. |
| `LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING` or `ERROR`; any case. |
| `SENTRY_DSN` | production, secret | Sentry errors and security events; off when unset. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | production | OTLP/HTTP base URL for traces and metrics (`https` in staging/production); off when unset. |
| `OTEL_EXPORTER_OTLP_HEADERS` | no, secret | `name=value` pairs, comma-separated, percent-encoded as in Grafana's snippet (`Authorization=Basic%20…`). |
| `OTEL_TRACES_SAMPLER_RATIO` | no | Share of new traces recorded, `0`–`1` (default `1`). Client-sent trace context is ignored. |
| `OBSERVABILITY_REQUIRED` | no | `true` by default: production refuses to start without `SENTRY_DSN` and `OTEL_EXPORTER_OTLP_ENDPOINT`. `false` opts out (startup still warns). |
| `OTEL_SERVICE_NAME` | no | `gize-admin-api` by default. |
| `RELEASE` | no | Build identifier (e.g. git SHA) for Sentry and traces. |
| `DATABASE_URL` | yes, secret | Postgres as `gize_app`. `ssl=verify-full` in production; `ssl=require` or `verify-full` in staging. Direct connection or session-mode pooler (port 5432); transaction mode (6543) is not supported. |
| `DATABASE_SSL_ROOT_CERT` | production (Supabase) | Path to the CA certificate (PEM) the database certificate must chain to, with `ssl=verify-full`. For Supabase, the certificate from the project's database settings (SSL configuration), mounted into the container. Unset: system CAs. |
| `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` | no | Connections per process, 5 and 5 by default (1–50, 0–50). See the connection budget below. |
| `MIGRATOR_DATABASE_URL` | migrations, secret | Postgres as `gize_migrator`; used by Alembic and the image's `migrate` command, never by the API. Same TLS rules as `DATABASE_URL` (by `APP_ENV`, with `DATABASE_SSL_ROOT_CERT`); direct or session-mode connection only. |
| `BOOTSTRAP_ADMIN_DATABASE_URL` | bootstrap only, secret | The `postgres` role, for the one-off bootstrap. |
| `TEST_ADMIN_DATABASE_URL` | tests only | A superuser on the compose **test** server (`postgres-test`, port 5433), never the development one: tests set the role passwords to test values. Tests create and drop their own databases. |
| `TEST_REDIS_URL` | tests only | Redis the test suite uses (CI sets it; locally read from `.env`). Tests fail, never skip, without it. |

## Database

Postgres (Supabase in staging and production), schema `gize`, two roles:

| Role | Can | Used by |
| --- | --- | --- |
| `gize_migrator` | owns schema `gize`: create, alter, drop | Alembic (`MIGRATOR_DATABASE_URL`, the image's `migrate` command) |
| `gize_app` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` on data tables; nothing else (no DDL, no `TRUNCATE`, no migration table) | the API (`DATABASE_URL`) |

Neither role is superuser, creates roles or databases, bypasses row-level security, is a member of another role, or has an expiring password. `gize_app` has role-level settings: statement timeout 5 s, lock 3 s, idle in transaction 10 s, `search_path` `gize`.

What keeps Supabase's API roles out:
- **No access to the schema.** `anon`, `authenticated`, `service_role` and PUBLIC have no `USAGE` on `gize`, and no one but `gize_app` may execute new functions. This is the guarantee for all three.
- **Row-level security** on every table, with a single policy for `gize_app`: a mistaken grant to `anon` or `authenticated` still exposes no rows. It does **not** stop `service_role`, which bypasses RLS on Supabase (the local stand-in does too).
- Views must be created `WITH (security_invoker = true)`, or they would read tables as their owner and skip RLS (a test checks).

Accepted and known: `gize_app`, like every role, can create temporary tables, connect to other databases on the server and change its own session settings and password, because Supabase's own services need those privileges for PUBLIC. It cannot change any table, role or schema.

The bootstrap refuses to run if it finds drift (a membership, `CREATE` on the schema or database, `TRUNCATE`/`REFERENCES`/`TRIGGER`/`MAINTAIN` on a table, an object owned by `gize_app`) and names it; it resets the roles' settings and password expiry. After the first run, table grants belong to migrations: re-running never re-grants on existing tables. Create objects in `gize` only through migrations: anything made by hand (the dashboard) is owned by `postgres` and gets neither the default grants nor RLS.

**Connection budget.** Each process opens at most `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW` connections (10 by default). Add up every process that can run at once (API replicas, the background worker, the previous release during a deploy, a migration) and keep the total under the pooler's client limit for your compute size (shown in the Supabase dashboard's database settings).

**Setting up a Supabase project (once, by a human):**
1. In the dashboard, keep `gize` out of the Data API's exposed schemas (Settings → API).
2. Download the project's CA certificate (database settings → SSL configuration) and make it available to the API and the release step as a file; set `DATABASE_SSL_ROOT_CERT` to its path.
3. Choose passwords for `gize_app` and `gize_migrator` and write the two URLs (Supavisor session pooler, port 5432; usernames `gize_app.<project-ref>` and `gize_migrator.<project-ref>`; append `?ssl=verify-full`) into the host's secret store.
4. Run the bootstrap from a trusted machine with `BOOTSTRAP_ADMIN_DATABASE_URL` set to the `postgres` connection string: `uv run python -m gize_api.core.bootstrap` (no `--create-supabase-roles`: Supabase has them). It is safe to run again; passwords are sent as SCRAM verifiers, never in plain text. It waits at most 5 s for a lock held by someone else, then stops.
5. Release step on every deploy, before the new API version starts: `docker run … gize-admin-api migrate` with `APP_ENV`, `MIGRATOR_DATABASE_URL` and `DATABASE_SSL_ROOT_CERT`. Only one migration runs at a time: a second one stops with "another migration is running".

**Migrations** are expand/contract: add columns nullable or with defaults, backfill separately, remove only in a later release. Every migration has a working `downgrade()` (CI runs upgrade → downgrade → upgrade) and every new table calls `enable_row_level_security(op, "<table>")`. Import every new `models.py` in `src/gize_api/models.py` (a test checks), so `alembic check` and `revision --autogenerate` compare all of schema `gize` and nothing else. New revision files are cleaned by ruff as they are written.

## Logs, traces and errors

The API writes one JSON object per line to stderr: `timestamp`, `level`, `logger`, `event`, `request_id` and (once authenticated) `staff_id`, plus one `request` line per request with the route template, status and duration. Secrets and personal data are scrubbed before anything is written: tokens, passwords, OTPs and names become `[REDACTED]`, phone numbers become `+2519••••1234`. Use `structlog.get_logger(__name__)` and pass data as fields, never formatted into the message.

With `SENTRY_DSN` and `OTEL_EXPORTER_OTLP_ENDPOINT` set, unhandled errors and security events go to Sentry (tagged with the request ID), and traces and metrics go to the OTLP endpoint. Production refuses to start without both unless `OBSERVABILITY_REQUIRED=false`; staging logs `observability_disabled` for each one that is unset. Log lines inside a request carry `trace_id`, and spans carry `gize.request_id`, so logs, traces and Sentry events can be matched.

Call `set_staff_id()` from the (async) auth dependency to put the staff ID on that request's log lines; call `log_security_event()` for failed auth, permission denials, rate-limit hits and OTP abuse.

## Docker image

One image serves both processes:

```bash
docker build -t gize-admin-api .
docker run --rm -p 8000:8000 -e APP_ENV=local \
  -e REDIS_URL=redis://host.docker.internal:6379/0 gize-admin-api   # api (default)
# On Linux, also pass --add-host=host.docker.internal:host-gateway
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
| Migrations (upgrade, downgrade, upgrade) | the bootstrap fails or changes on a second run (run as a non-superuser admin, as on Supabase), any migration fails, a downgrade cannot be undone, or models and migrations disagree (`alembic check`) |
| Docker image build | the image does not build, does not run as uid 10001, or cannot import the app |

There is no branch protection on our plan, so CLAUDE.md's merge rules are the protection. Before merging, confirm **all five checks above are listed and green** on the PR: `gh pr checks` only shows checks that ran, so a job deleted from the workflow would not show up as failing. `tests/test_ci_config.py` pins the job list, and any change under `.github/workflows/` needs an explicit reviewer look.

**Coverage ratchet.** `.coverage-baseline` holds `main`'s total coverage. CI fails when coverage drops and also when it rises without the baseline being raised, so a later PR cannot quietly give the gain back. When coverage improves, run `uv run python -m scripts.check_coverage_baseline --update` and commit the file. Lowering it is a hand edit the PR must justify.

**If gitleaks finds a real secret** that already reached `main`: rotate the secret first (it is compromised whatever happens to the history), then add the finding's fingerprint (printed by gitleaks) to `.gitleaksignore` with a comment naming the rotation, in its own PR.

## Working on a ticket

Follow [`docs/workflow.md`](docs/workflow.md): one ticket, one branch (`feat/GZ-xxx-short-name`), one PR titled `GZ-xxx: <title>` using the PR template. Never push to `main` directly.
