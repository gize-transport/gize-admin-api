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
| Run the background worker locally | `uv run --env-file .env python -m gize_api.worker` |
| Tests with coverage (85% overall) | `uv run pytest` |
| Services coverage gate (90%), after tests | `uv run python -m scripts.check_service_coverage` |
| Lint, format check, types | `uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| All pre-commit hooks | `uv run pre-commit run --all-files` |
| Coverage must not drop, after tests | `uv run python -m scripts.check_coverage_baseline` (`--update` raises the baseline) |
| Apply / undo migrations | `uv run --env-file .env alembic upgrade head` / `… alembic downgrade -1` |
| New migration | `uv run --env-file .env alembic revision --autogenerate --rev-id 0003 -m "short name"` |
| Dependency audit | `uv export --frozen --all-groups --no-emit-project --format requirements-txt \| uv run pip-audit --strict --disable-pip --require-hashes -r /dev/stdin` |

The OpenAPI export command arrives with GZ-015.

## Configuration

All configuration comes from environment variables (`src/gize_api/core/config.py`); the API refuses to start when a required one is missing or invalid, and the error names the variable, never its value. `.env.example` lists every variable with a placeholder.

| Variable | Required | Notes |
| --- | --- | --- |
| `APP_ENV` | yes | `local`, `test`, `staging` or `production`. Docs (`/docs`, `/redoc`, `/openapi.json`) are served only in `local` and `staging`. |
| `REDIS_URL` | yes, secret | `redis://` or `rediss://` URL (with `rediss`, the server's certificate and host name are checked). |
| `SUPABASE_URL` | yes | The Supabase project URL, `https://<project-ref>.supabase.co` (Project Settings → API). Sign-in tokens are verified against its public signing keys and must name its Auth service as issuer. |
| `SUPABASE_SECRET_KEY` | for the first-admin command (and staff invites, GZ-013) | **Secret.** The project's secret API key, `sb_secret_...` (Project Settings → API Keys → Secret keys). It can read and create users: keep it in `.env` or the host's secret store, never in chat, code or the frontend. The legacy `service_role` JWT is refused. |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated origins (`scheme://host[:port]`, lowercase, no path); `https` only in `staging` and `production`. Empty: no cross-origin access. |
| `CLIENT_IP_HEADER` | no | The header a trusted proxy in front of the API writes the client's IP into: `CF-Connecting-IP` behind Cloudflare, or your load balancer's header. Empty (default): the connection's address. Set it only when the API accepts connections **from that proxy alone** (a firewall, or Cloudflare's authenticated origin pulls): a client reaching the API directly could otherwise send any value and dodge every per-IP limit. Behind a load balancer without it, every client shares the balancer's IP. |
| `LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING` or `ERROR`; any case. |
| `SENTRY_DSN` | production, secret | Sentry errors and security events; off when unset. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | production | OTLP/HTTP base URL for traces and metrics (`https` in staging/production); off when unset. |
| `OTEL_EXPORTER_OTLP_HEADERS` | no, secret | `name=value` pairs, comma-separated, percent-encoded as in Grafana's snippet (`Authorization=Basic%20…`). |
| `OTEL_TRACES_SAMPLER_RATIO` | no | Share of new traces recorded, `0`–`1` (default `1`). Client-sent trace context is ignored. |
| `OBSERVABILITY_REQUIRED` | no | `true` by default: production refuses to start without `SENTRY_DSN` and `OTEL_EXPORTER_OTLP_ENDPOINT`. `false` opts out (startup still warns). |
| `OTEL_SERVICE_NAME` | no | `gize-admin-api` by default. |
| `RELEASE` | no | Build identifier (e.g. git SHA) for Sentry and traces. |
| `DATABASE_URL` | yes, secret | Postgres as `gize_app`. `ssl=verify-full` in staging and production (staging verifies too, so a certificate problem shows up before launch). Direct connection or session-mode pooler (port 5432); transaction mode (6543) is not supported. |
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

The bootstrap resets the roles' settings (role-wide and for this database) and password expiry. It refuses to run, naming the problem, if it finds drift: a membership either way (our roles in another role, or another role in ours; the admin may keep ADMIN OPTION only), role settings for another database, `CREATE` on schema `gize` or `public` or on the database, `TRUNCATE`/`REFERENCES`/`TRIGGER`/`MAINTAIN` on a table, or an object owned by `gize_app`. After the first run, table grants belong to migrations: re-running never re-grants on existing tables. Create objects in `gize` only through migrations: anything made by hand (the dashboard) is owned by `postgres` and gets neither the default grants nor RLS.

**Connection budget.** Each process opens at most `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW` connections (10 by default); each API instance adds one more for its readiness check (and one Redis connection). Add up every process that can run at once (API replicas, the background worker, the previous release during a deploy, a migration) and keep the total under the pooler's client limit for your compute size (shown in the Supabase dashboard's database settings).

**Setting up a Supabase project (once, by a human):**
1. In the dashboard, keep `gize` out of the Data API's exposed schemas (Settings → API).
2. Download the project's CA certificate (database settings → SSL configuration) and make it available to the API and the release step as a file; set `DATABASE_SSL_ROOT_CERT` to its path.
3. Choose passwords for `gize_app` and `gize_migrator` and write the two URLs (Supavisor session pooler, port 5432; usernames `gize_app.<project-ref>` and `gize_migrator.<project-ref>`; append `?ssl=verify-full`) into the host's secret store.
4. Run the bootstrap from a trusted machine with `BOOTSTRAP_ADMIN_DATABASE_URL` set to the `postgres` connection string: `uv run python -m gize_api.core.bootstrap` (no `--create-supabase-roles`: Supabase has them). Off your own machine the admin connection must use `?ssl=verify-full`, and `DATABASE_SSL_ROOT_CERT` must point at the downloaded certificate: this session can create roles and rewrite grants, so it is never left unverified. It needs Postgres 17 or later. It is safe to run again; passwords are sent as SCRAM verifiers, never in plain text. If the project sets `createrole_self_grant`, the admin's automatic membership in the new roles is reduced to ADMIN OPTION (it can manage the roles, not act as them). It waits at most 5 s for a lock held by someone else, then stops.
5. Release step on every deploy, before the new API version starts: `docker run … gize-admin-api migrate` with `APP_ENV`, `MIGRATOR_DATABASE_URL` and `DATABASE_SSL_ROOT_CERT`. Only one migration runs at a time: a second one stops with "another migration is running".

**Migrations** are expand/contract: add columns nullable or with defaults, backfill separately, remove only in a later release. Every migration has a working `downgrade()` (CI runs upgrade → downgrade → upgrade) and every new table calls `enable_row_level_security(op, "<table>")`. Import every new `models.py` in `src/gize_api/models.py` (a test checks), so `alembic check` and `revision --autogenerate` compare all of schema `gize` and nothing else. New revision files are cleaned by ruff as they are written.

## Logs, traces and errors

The API writes one JSON object per line to stderr: `timestamp`, `level`, `logger`, `event`, `request_id` and (once authenticated) `staff_id`, plus one `request` line per request with the route template, status and duration. Secrets and personal data are scrubbed before anything is written: tokens, passwords, OTPs and names become `[REDACTED]`, phone numbers become `+2519••••1234`. Use `structlog.get_logger(__name__)` and pass data as fields, never formatted into the message.

With `SENTRY_DSN` and `OTEL_EXPORTER_OTLP_ENDPOINT` set, unhandled errors and security events go to Sentry (tagged with the request ID), and traces and metrics go to the OTLP endpoint. Production refuses to start without both unless `OBSERVABILITY_REQUIRED=false`; staging logs `observability_disabled` for each one that is unset. Log lines inside a request carry `trace_id`, and spans carry `gize.request_id`, so logs, traces and Sentry events can be matched. A database that is down, saturated or asks for a retry answers 503 with `Retry-After` and is logged as `database_unavailable` with its SQLSTATE and counted (`gize.db.unavailable`); it goes to Sentry only when a human must act (wrong password or role, unknown database, an untrusted certificate), at most once a minute per process and kind. Network errors count only when raised on the way through the database driver: an outbound call or Redis failing is never reported as the database. Every Redis operation of ours (rate-limit checks, job enqueueing, the readiness ping) records `db.client.operation.duration` with `db.system.name=redis`, the operation and its outcome; the Redis instrumentation itself records spans only.

Call `set_staff_id()` from the (async) auth dependency to put the staff ID on that request's log lines; call `log_security_event()` for failed auth, permission denials, rate-limit hits and OTP abuse.

## Docker image

One image serves both processes:

```bash
docker build -t gize-admin-api .
docker run --rm -p 8000:8000 -e APP_ENV=local \
  -e REDIS_URL=redis://host.docker.internal:6379/0 gize-admin-api   # api (default)
# On Linux, also pass --add-host=host.docker.internal:host-gateway
docker run --rm -e APP_ENV=local -e DATABASE_URL=… \
  -e REDIS_URL=redis://host.docker.internal:6379/0 gize-admin-api worker   # background worker
```

The worker runs as `python -m gize_api.worker`, never Arq's own CLI, which would replace the JSON logging with plain text that prints job arguments.
- On SIGTERM it stops taking jobs, gives running ones 20 s and re-queues the rest; closing the database and flushing telemetry still fit in the 30 s grace period.
- A failed job is logged (type and frames), sent to Sentry, and stored in Redis only as `JobFailedError`, never its message.
- Jobs travel as JSON, never pickle (with pickle, anyone able to write to Redis could run code in the worker).

The container runs as an unprivileged user (uid 10001). The API finishes in-flight requests for up to 30 s after SIGTERM, so give the container a stop timeout of at least 35 s on whichever host runs it (Docker's default is 10 s: `docker stop -t 35`, or `stop_grace_period: 35s` in Compose).

## Building blocks for features

- **Lists:** `core/pagination.py`. `?limit=&cursor=` (limit 1–100, default 25), `{items, next_cursor}`; ordered by `(created_at, id)`, so list tables need an index on those two columns. A tampered cursor is `422 INVALID_CURSOR`.
- **Side-effecting POSTs:** `core/idempotency.py`. The `Idempotency-Key` header (a UUID) is required (`428 IDEMPOTENCY_KEY_REQUIRED`).
  - The service calls `claim` as the **first** statement of its transaction block and `record` before the block ends. A retry replays the stored status, body and `Location`/`ETag` headers, with `Idempotent-Replayed: true`.
  - A failure must **raise** (an `AppError` for a client error): the rollback frees the key. A claim left unrecorded makes the commit fail.
  - The same key with a different body, path ID or query is `422 IDEMPOTENCY_KEY_MISMATCH`. While the first request is still running it is `409 IDEMPOTENCY_IN_PROGRESS`.
  - Keys live 24 h; the worker deletes expired ones hourly.
- **Calling outside services:** `core/http.py` `OutboundClient`, never `httpx` directly.
  - https only, to the hosts the integration allows (port 443 unless the entry says `host:port`), at public addresses only (checked, then connected to). NAT64 and 6to4 addresses are judged by the IPv4 address they carry.
  - Each host gets its own connection pool, so every host's certificate is checked.
  - Callers cannot set framing headers.
  - The whole call takes at most 10 s; responses are requested uncompressed and capped at 1 MB; only safe methods are retried; no trace headers are sent.
- **Background jobs:** `core/jobs.py` `enqueue(pool, "job_name", *ids, job_id=…)`, after the transaction commits. A repeated job ID is refused for 24 h. Jobs take IDs, never personal data.
- **Never** call out or enqueue inside `async with session.begin():`: both raise `OpenTransactionError` (tracked per task, for sessions; raw engine connections are not app code).
- **Rate limits:** `core/ratelimit.py`, built on `limits`' async API, so it never blocks on Redis. (slowapi was replaced at GZ-009; it calls Redis synchronously.)
  - Routes add `Depends(rate_limited(POLICY, after=<auth dependency>))`. It counts by staff ID, or IP before login; `after=` makes auth run first (FastAPI runs decorator dependencies before parameter ones).
  - Services check phone limits with `await request.app.state.rate_limiter.check(POLICY, phone=...)`, passing a normalised E.164 number: `0911…` and `+251911…` would otherwise be separate counters.
  - IPv6 clients are counted by their /64; IPv4-mapped addresses as IPv4.
  - Limits are checked in order and each check counts, so a request rejected by a later limit still uses up the earlier ones.
  - Policies (engineering-standards.md): `STAFF` 120/min, `PUBLIC_SIGNUP` 5/hour per IP and 3/day per phone, `SMS` 10/hour per IP and 3/hour per phone. An expensive endpoint defines its own.
  - Over the limit: `429 RATE_LIMITED` with `Retry-After`, logged as a security event (the policy, never the key). Sentry hears at most once a minute per policy and process.
  - Redis unavailable: staff policies allow the request (logged once a minute per process, and counted as `outcome=error` in `db.client.operation.duration`: alert on it); public and SMS policies answer `503` with `Retry-After`. A busy connection pool waits instead of failing; one 1 s deadline covers each check.
  - Keys reach Redis hashed, so no phone number or IP is stored in clear. The hash is not secret, so it stops casual reading only.
  - Health endpoints have no limits.

## Sign-in tokens

Staff sign in to Supabase Auth in the admin portal, which sends `Authorization: Bearer <access token>`. `core/security.py` verifies it, and `verified_token` is the FastAPI dependency (`current_staff`, below, builds on it).

- **Asymmetric signing keys only** (ES256 or RS256). In the Supabase dashboard (Project Settings → JWT Keys) the project must use signing keys, not the legacy shared JWT secret: the API never holds a secret that could create tokens. A project still on the shared secret issues HS256 tokens, which are all rejected (`401`, reason `algorithm`).
- **Checked:**
  - the signature against the project's key set (`/auth/v1/.well-known/jwks.json`);
  - `exp` and `nbf` with 30 s of clock difference;
  - `aud = authenticated` and `iss = <project>/auth/v1`;
  - a UUID `sub`, `role = authenticated` (no anon or service-role tokens), and not an anonymous sign-in.
  - Not checked: `aal` (MFA). A token stays valid after sign-out until it expires (1 h by default); GZ-011's active-staff check on every request is what revokes access.
- **Keys** are fetched through the guarded outbound client and cached for 10 minutes. Only public signature keys count.
  - Refreshes happen at most every 30 s, one at a time, shared by concurrent requests. Forged key IDs can't flood Supabase.
  - During an outage, cached keys are served at once while one refresh runs in the background, for up to 24 hours. Without any, requests get `503` with `Retry-After` at once.
  - **Revoking a key urgently:** a key revoked in Supabase stays trusted until the next successful refresh (within 10 minutes, or 24 hours if Supabase is unreachable). Restarting the API instances drops it immediately.
- **Rejected tokens** get `401 UNAUTHENTICATED` with `WWW-Authenticate: Bearer`. Each is logged as a `token_rejected` security event (the reason only, never the token; Sentry at most once a minute per reason and process).
- **Throttle:** after 30 *forged-looking* rejections a minute from one IP (bad signature, unknown key, wrong audience or issuer, malformed), that IP gets `429`. Missing or expired tokens (a stale tab) and valid tokens are never counted, so one stale tab can't lock out an office.

## Staff and roles

A valid token only proves someone has a Supabase account. Whether they are Gize staff, and in which role, comes from the `gize.staff` table (ID = Supabase user ID).

- **`current_staff`** (FastAPI dependency) looks the caller up on every request:
  - no row → `403 FORBIDDEN`, security event `staff_access_denied` (reason `not_staff`);
  - a deactivated row → `403`, reason `inactive`. **Deactivating someone locks them out on their next request,** even though their token is still valid;
  - both refusals count toward the per-IP token throttle (30 a minute), so a stranger or a dismissed employee can't send unlimited requests. Only refusals count: active staff are never throttled by it;
  - the role (`admin` or `staff`) always comes from the table, never from the token.
- **`require_role(StaffRole.ADMIN)`** → `403`, security event `permission_denied`, for any other role.
- **Staff routes** are built on `staff_router(...)`, which applies the `STAFF` limit (120 a minute per staff member; Redis down → allowed). A test fails if any route the app serves lacks `current_staff` or that limit, unless its method and path are on the public list (health checks, the local and staging docs, and later the public sign-up routes).
- **Object-level checks** live in services (see `docs/architecture.md`).
- **`GET /v1/staff/me`** returns the caller's `id`, `full_name`, `email` and `role` (`Cache-Control: no-store`). The admin portal calls it after sign-in.
- **Supabase sign-ups:** staff are added by admins (GZ-013). In the Supabase dashboard (Authentication → Sign In / Providers), turn off **Allow new users to sign up**, so strangers can't even get a token. For the very first admin, create the user in the dashboard (Authentication → Users → Add user), then make them admin with the GZ-014 command.

## First admin

Admins add staff in the admin portal, so the very first admin is made by a command, once per environment.

1. **Create the user** in the Supabase dashboard: Authentication → Users → Add user. The email must end up confirmed (setting a password alone doesn't confirm it):
   - **Create new user**, with **Auto Confirm User** ticked; or
   - **Send invitation**, and wait until they accept it.
2. **Copy their UID** from the user's row in that list.
3. **Get the secret key:** Project Settings → API Keys → Secret keys (`sb_secret_...`). A project still on the legacy keys has none yet: create one there first.
4. **Set up the environment** where the command runs: the app's full environment (`APP_ENV`, `DATABASE_URL` for that environment's database, `REDIS_URL`, and so on) plus `SUPABASE_URL` and `SUPABASE_SECRET_KEY`. Locally that is your `.env`; on a host, the API's secret store.
5. **Run:**
   ```bash
   uv run --env-file .env python -m gize_api.cli.first_admin --user-id <UID> --name "Full Name"
   ```
   On a host, run it inside the running API container (`docker exec <container> python -m gize_api.cli.first_admin ...`). The image's entrypoint only starts `api`, `worker` or `migrate`, so `docker run` with this command doesn't work.

The command reads the user from Supabase (it must exist, have a confirmed email, and not be banned, deleted or anonymous). It then makes them an active admin in one transaction, and refuses if any admin already exists, active or not. Two runs at once can't both succeed. It prints only the user ID, never names or emails, and reports the new admin to Sentry (a `first_admin_created` security event) until the audit log exists.

Exit codes:
- `0`: done.
- `1`: refused, another run in progress, or Supabase or the database unavailable. If the database dropped mid-run, the change may or may not have been saved; running the command again is safe, since it refuses once an admin exists.
- `2`: wrong arguments or settings.

## Health checks

For the load balancer or orchestrator, without auth, never cached, not in the API schema:

| Endpoint | 200 when | Otherwise |
| --- | --- | --- |
| `GET`/`HEAD /health/live` | the process is up (checks nothing else) | — |
| `GET`/`HEAD /health/ready` | Postgres answers within 1 s | 503 problem+json, `Retry-After: 5` |

Use `/health/live` to restart a stuck instance and `/health/ready` to route traffic. Redis is checked alongside but never fails readiness: one shared Redis outage would otherwise take every instance out of rotation (engineering-standards.md, "Health").

- The checks use one Postgres and one Redis connection of their own, never the app's pools, so a busy instance at peak still reads as ready.
- A probe waits at most 1 s, even when the server hangs on an open connection, and one result serves all probes for 1 s.
- An outage is logged at ERROR as `dependency_unavailable` (with `dependency` and the error type) when it starts and once a minute while it lasts, and `dependency_recovered` when it ends. Every failed check is counted in `gize.dependency.unavailable`; alert on it.
- Successful probes are left out of the access log and traces, and counted in `gize.health.probes` (by endpoint and status).

The image declares a Docker `HEALTHCHECK` (every 10 s, 3 retries, 20 s start period). It tells the containers apart by their processes, also under an init process (`docker run --init`, ECS):
- **API:** probes `/health/live`.
- **Worker:** checks the health key this worker renews in Redis every 10 s (one key per replica, named after its host name), so a hung worker becomes unhealthy whatever its replicas do.
- **Migration:** healthy.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

| Check | What fails it |
| --- | --- |
| Lint, types, dependency audit | `uv.lock` out of date; any pre-commit hook (ruff, ruff format, mypy `--strict`, file hygiene, TODO without a ticket); `pip-audit` finds a known vulnerability in any locked dependency |
| Secret scan (gitleaks) | gitleaks finds a secret in any commit reachable from the PR |
| Tests and coverage gates | a failing test; total coverage < 85%; service modules < 90%; total coverage differs from `.coverage-baseline` |
| Migrations (upgrade, downgrade, upgrade) | the bootstrap fails or changes on a second run (run as a non-superuser admin, as on Supabase), any migration fails, a downgrade cannot be undone, or models and migrations disagree (`alembic check`) |
| Docker image build | the image does not build, does not run as uid 10001, or cannot import the app |
| API contract (up to date, no breaking changes) | `openapi.json` differs from the code; the breaking-change gate's self-test fails; `oasdiff` finds a breaking change against the base branch and the PR lacks the `breaking-change-approved` label |

There is no branch protection on our plan, so CLAUDE.md's merge rules are the protection. Before merging, confirm **all six checks above are listed and green** on the PR: `gh pr checks` only shows checks that ran, so a job deleted from the workflow would not show up as failing. `tests/test_ci_config.py` pins the job list, and any change under `.github/workflows/` needs an explicit reviewer look.

**Coverage ratchet.** `.coverage-baseline` holds `main`'s total coverage. CI fails when coverage drops and also when it rises without the baseline being raised, so a later PR cannot quietly give the gain back. When coverage improves, run `uv run python -m scripts.check_coverage_baseline --update` and commit the file. Lowering it is a hand edit the PR must justify.

**If gitleaks finds a real secret** that already reached `main`: rotate the secret first (it is compromised whatever happens to the history), then add the finding's fingerprint (printed by gitleaks) to `.gitleaksignore` with a comment naming the rotation, in its own PR.

## API contract

`openapi.json` at the repo root is the contract with the admin portal (`gize-admin-web`); the rules are in `docs/api-contract.md`.

- **Regenerate it with every API change** and commit it: `uv run python scripts/export_openapi.py`. CI fails when it's out of date.
- **Every operation** needs a stable `operationId` (`<domain>_<action>`, e.g. `staff_me`), a summary, a response model, and its errors via `problem_responses(...)` (`core/errors.py`). Staff routers already document 401/403/429/503; 422 (wherever there's input), 413 (wherever there's a body) and 500 are added for you (`core/contract.py`). Each error response is `application/problem+json` and lists its codes in `x-error-codes`. `tests/test_contract.py` holds every operation to this.
- **Breaking changes fail CI** (`oasdiff breaking --fail-on WARN` against the base branch): removing or renaming anything, making a field required, changing a type, tightening a limit. Follow the deprecation steps in `docs/api-contract.md`. Only the owner adds the `breaking-change-approved` label to let one through.
- **Releases are automatic.** When a merge to `main` changes `openapi.json`, `.github/workflows/contract-release.yml` tags `api-vX.Y.Z` and attaches the file to a GitHub release:
  - minor for any change oasdiff reports (an added endpoint, field or enum value);
  - patch for descriptions and summaries only;
  - major for an approved breaking change.

  The frontend syncs from these tags. The first release is `api-v1.0.0`.

## Working on a ticket

Follow [`docs/workflow.md`](docs/workflow.md): one ticket, one branch (`feat/GZ-xxx-short-name`), one PR titled `GZ-xxx: <title>` using the PR template. Never push to `main` directly.
