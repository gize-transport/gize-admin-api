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
| `SUPABASE_SECRET_KEY` | for the first-admin command and staff invites (without it, invites answer `503`) | **Secret.** The project's secret API key, `sb_secret_...` (Project Settings → API Keys → Secret keys). It can read and create users: keep it in `.env` or the host's secret store, never in chat, code or the frontend. The legacy `service_role` JWT is refused. |
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
  - Policies (engineering-standards.md): `STAFF` 120/min, `STAFF_INVITE` 20/hour per admin (each invite sends an email), `PUBLIC_SIGNUP` 5/hour per IP and 3/day per phone, `SMS` 10/hour per IP and 3/hour per phone. An expensive endpoint defines its own.
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
  - a member's first request records `first_signed_in_at` (once): until then, an invited member hasn't accepted. The staff list shows it.
- **`require_role(StaffRole.ADMIN)`** → `403`, security event `permission_denied`, for any other role.
- **Staff routes** are built on `staff_router(...)`, which applies the `STAFF` limit (120 a minute per staff member; Redis down → allowed). A test fails if any route the app serves lacks `current_staff` or that limit, unless its method and path are on the public list (health checks, the local and staging docs, and later the public sign-up routes).
- **Object-level checks** live in services (see `docs/architecture.md`).
- **`GET /v1/staff/me`** returns the caller's `id`, `full_name`, `email` and `role` (`Cache-Control: no-store`). The admin portal calls it after sign-in.
- **Supabase sign-ups:** staff are added by admins (below). In the Supabase dashboard (Authentication → Sign In / Providers), turn off **Allow new users to sign up**, so strangers can't even get a token. For the very first admin, create the user in the dashboard (Authentication → Users → Add user), then make them admin with the GZ-014 command.

## Staff management

Admins manage staff under `/v1/staff` (GZ-013). Every route is for **active admins only**; personal data, so `Cache-Control: no-store`.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/staff` (`staff_invite`) | Body `{email, full_name, role}`. Supabase emails them an invitation; they are active staff in that role at once and can sign in when they have accepted. `201` with `Location`. Requires `Idempotency-Key`; 20 invites an hour per admin (refusals and replays don't count). |
| `GET /v1/staff` (`staff_list`) | Newest first, cursor-paginated, filtered by `role` and `is_active`. Deactivated members are listed; `first_signed_in_at` is null while an invitation is pending. |
| `GET /v1/staff/{staff_id}` (`staff_get`) | One member, or `404`. |
| `PATCH /v1/staff/{staff_id}` (`staff_update`) | Body `{role?, is_active?}`, at least one, never `null`. Promote, demote, deactivate (locks them out on their next request) or reactivate. No key needed: setting what is already there changes nothing. |

- **Emails** are stored trimmed and lower-case. An email already on staff, active or not, is `409 STAFF_EMAIL_TAKEN`: reactivate the person instead.
- **Existing sign-in accounts:** an email that already has a confirmed Supabase account is `409 AUTH_ACCOUNT_EXISTS` (owner decision, GZ-013). Turning an existing account into staff is deliberate work for an operator, not a side effect of an invite.
  - **Recovery** (for example, someone accepted an invite whose write then failed, so they have an account but no staff row and get `403`): in the Supabase dashboard (Authentication → Users) delete that user, then invite them again. Check first that the account is the stranded invitee and not someone else's.
- **Public sign-ups must stay off** (see "Staff and roles"). An invite to an email with an *unconfirmed* Supabase account re-sends to that account; with sign-ups on, a stranger could create that account first with a password of their own.
- **Always an admin:** no change may leave the system without an active admin who has signed in at least once (`409 LAST_ACTIVE_ADMIN`). An invited admin who hasn't accepted yet doesn't count: with a mistyped email they never will, and nobody could sign in as admin (owner decision, GZ-013). Every staff change, and the first-admin command, holds one database lock (`lock_staff_changes`), so two admins stepping down at once can't both succeed. The service re-reads the caller under that lock: an admin demoted or deactivated a moment ago is refused.
- **Invites and failures:** Supabase is called between two transactions, never inside one.
  - A retry of a finished invite (same key) replays the stored response without calling Supabase.
  - If Supabase answered but the database write failed, a retry invites again: Supabase returns the same user (a second email arrives) and the write completes.
  - Supabase unreachable or refusing gives `503`; nothing is stored. `Retry-After` is 30 s for a passing failure and Supabase's own wait (at most an hour) when its email limit is reached. There is none when only a configuration fix helps (the secret key refused or missing, custom SMTP needed). The log line `supabase_invite_failed` says why (for example, Supabase's built-in email refusing an address outside the project team).
- **Audit and alerts:** `staff.invited`, `staff.role_changed`, `staff.deactivated`, `staff.reactivated` in the audit log (role and status by value, name and email redacted). The matching security events carry IDs and roles only. Making someone an admin, by invite, promotion or reactivation, also alerts in Sentry.
- **Supabase setup before real invites** (owner, in the Supabase dashboard):
  - **Site URL** (Authentication → URL Configuration): the admin portal's page where invited staff set their password, once the portal has it. Until then invite links land on Supabase's default.
  - **Custom SMTP** (Authentication → Emails → SMTP Settings): Supabase's built-in email only reaches addresses of the project's team, with a very low hourly limit.

## Areas

The list of neighbourhoods and landmarks everyone picks from (GZ-026), so "Ayat" typed three ways is one place. Corridors, the areas routes serve, route requests and riders refer to areas by ID.

| Endpoint | Who | What it does |
| --- | --- | --- |
| `POST /v1/areas` (`areas_create`) | staff | `{name}`. Requires `Idempotency-Key`. `201` with `Location`. |
| `GET /v1/areas` (`areas_list`) | staff | Newest first, cursor-paginated. `q` finds names starting with it, ignoring case. |
| `GET /v1/areas/{area_id}` (`areas_get`) | staff | One area. |
| `PATCH /v1/areas/{area_id}` (`areas_update`) | staff | Rename. |
| `DELETE /v1/areas/{area_id}` (`areas_delete`) | admin | Soft delete: hidden from lists, name free again, history kept. `204`. |

- Names are trimmed and kept as typed. They're unique ignoring case among areas that aren't deleted (`409 AREA_NAME_TAKEN`). Two people adding the same name at once get one area and one `409`: the database's index decides.
- Deleted areas answer `404`, like unknown ones.
- Every change is audited (`area.created`, `area.renamed`, `area.deleted`), by value: areas hold no personal data.
- Deleting an area a live corridor uses, a live route serves, or an open route request names is refused (`409 AREA_IN_USE`). Riders add their check in M3.

## Corridors

A corridor is a pair of areas, from (`origin`) and to (`destination`), that routes run along (GZ-020). Same roles as areas: staff manage, admins delete.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/corridors` (`corridors_create`) | `{origin_area_id, destination_area_id}`, two different live areas. Requires `Idempotency-Key`. `201` with `Location`. |
| `GET /v1/corridors` (`corridors_list`) | Newest first, with both areas' names; filters `origin_area_id`, `destination_area_id`. |
| `GET /v1/corridors/{corridor_id}` (`corridors_get`) | One corridor. |
| `PATCH /v1/corridors/{corridor_id}` (`corridors_update`) | Change one end or both (leave one out to keep it; never `null`). |
| `DELETE /v1/corridors/{corridor_id}` (`corridors_delete`) | Admins. Soft delete, `204`. |

- Each pair exists once among live corridors (`409 CORRIDOR_EXISTS`); the way back (Mexico → Ayat) is another corridor.
- An area that doesn't exist, or is deleted, is a `422` on its field.
- A corridor and an area delete never cross: creating a corridor, or changing an end, share-locks the area it now refers to, and deleting an area waits for that (and it for a delete). An end that stays is already counted as in use.
- Audited by value: `corridor.created`, `corridor.updated`, `corridor.deleted`.
- While a live route runs on a corridor, the corridor can't be deleted and its areas can't change (`409 CORRIDOR_IN_USE`; owner decision): move the routes to another corridor first. Route requests refer to areas, not corridors.


## Companies

Postpaid customers whose staff ride their own corporate routes (GZ-029). Staff manage companies, including deactivating one; admins delete them.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/companies` (`companies_create`) | `{name, contact_name, contact_phone, contact_email, notes?}`. Requires `Idempotency-Key`. Created active, billing `postpaid`. |
| `GET /v1/companies` (`companies_list`) | Newest first; `q` (names starting with it) and `is_active`. |
| `GET /v1/companies/{company_id}` (`companies_get`) | One company. |
| `PATCH /v1/companies/{company_id}` (`companies_update`) | Change any field except billing; `is_active: false` deactivates; empty `notes` clear them. |
| `DELETE /v1/companies/{company_id}` (`companies_delete`) | Admins. Soft delete, `204`. |

- **Names** are unique ignoring case among live companies (`409 COMPANY_NAME_TAKEN`), trimmed and normalised like area names.
- **Phones** may be typed any usual way (`0911 23 45 67`, `+251-911-234567`) and are stored E.164, `+2519…` or `+2517…` (`core/contact.py`, reused for riders in M3). Emails are stored trimmed and lower-case.
- **Audit:** `company.created`, `company.updated`, `company.deactivated`, `company.reactivated`, `company.deleted`. The contact details and notes are personal data, shown only as `[redacted]`; name, billing and status by value.
- A company can't be deactivated while one of its routes is active or paused, nor deleted while it has any live route (`409 COMPANY_IN_USE`).

## Routes

A route is one van from A to B, its corridor (GZ-021, GZ-023): the morning run leaves A and arrives at B at set times, the evening run back. Riders board at A or in an area the route serves on the way; there are no stops with their own times (owner decision). Staff create, list, read, edit and copy routes (a new van, GZ-028) and move them through their statuses (GZ-024); admins set prices (GZ-022) and delete routes.

| Endpoint | What it does |
| --- | --- |
| `POST /v1/routes` (`routes_create`) | `{code, corridor_id, route_type, school_name?, company_id?, morning_time, morning_arrival_time, return_time, return_arrival_time, seats, launch_minimum?}`. Requires `Idempotency-Key`. Created `draft`. |
| `GET /v1/routes` (`routes_list`) | Newest first, with corridor, areas, company and seat counts; filters `q` (code prefix), `route_type`, `status`, `corridor_id`, `company_id`. |
| `GET /v1/routes/{route_id}` (`routes_get`) | One route. |
| `PATCH /v1/routes/{route_id}` (`routes_update`) | Change code, corridor, school or company, times, seats, launch minimum. |
| `PUT /v1/routes/{route_id}/areas` (`routes_set_areas`) | The areas the route passes between A and B, in order: `{area_ids}`, the whole list (empty clears). Not A or B themselves, each once, at most 20. Shown on every route as `served_areas`. A route serving areas can't change corridor until they're cleared. |
| `POST /v1/routes/{route_id}/copies` (`routes_copy`) | Add a van: `{code, seats, launch_minimum}` (the launch minimum required for public and student routes, refused for corporate). Requires `Idempotency-Key`. Copies corridor, type, school or company, times, prices (never changed here) and served areas; starts `forming` when the original has both prices (corporate: `draft`), else `draft`; `copied_from_route_id` names the original, which may later be deleted (then a `404` if followed). `409 ROUTE_NOT_READY` for a corporate route whose company isn't active. Audited as `route.copied`. |
| `PUT /v1/routes/{route_id}/status` (`routes_set_status`) | `{status, launch_date?}`: move the route (see **Status** below). The status it already has changes nothing, so a retried launch is harmless. |
| `DELETE /v1/routes/{route_id}` (`routes_delete`) | **Admins.** Soft delete: hidden from lists, its code free again. `409 ROUTE_HAS_RIDERS` while anyone is reserved or rides today or later; `409 ROUTE_HAS_REQUESTS` while an open route request asks for it or was matched to it. Audited as `route.deleted`. |
| `PUT /v1/routes/{route_id}/prices` (`routes_set_prices`) | **Admins.** `{round_trip, one_way, saturday?}`, whole birr a month (1–100,000). No Saturday price means no Saturday add-on; only public routes have one (`422` on `saturday` otherwise). Corporate routes have no prices (`409 ROUTE_HAS_NO_PRICES`). Audited as `route.prices_changed`. |

- **Types** (fixed at creation, owner decision):
  - `public` (adults): a launch minimum; no school or company;
  - `student`: a school name and a launch minimum;
  - `corporate`: an active company; no launch minimum and no prices on the route (billed per company).
- **Codes** are letters, digits and dashes, 2–20, stored upper-case, unique among live routes (`409 ROUTE_CODE_TAKEN`).
- **Times** are local Addis Ababa `"HH:MM"`, in order: `morning_time` (leaves A), `morning_arrival_time` (at B), `return_time` (leaves B), `return_arrival_time` (at A). Riders get 5 minutes at A to board and the van may reach B up to 5 minutes late (fixed rules, checked once trips are recorded). Routes made before arrival times existed show them as `null` until both are set (one alone is a `422`).
- **Seats** (1–60) are counted per day: a rider holds a seat every day from their start date to their end date.
  - `seats_taken` and `seats_free` show the busiest day from today to the end of this month, and of next month. A rider leaving at the end of the month frees a seat for next month; company riders swapping mid-month take one seat.
  - `reserved` is launch progress ("9 of 12" with `launch_minimum`): riders reserved for the next launch day, today if it's the 1st, else the 1st of next month. Later reservations don't count yet.
  - Seats can't drop below seats taken on any day from today (`409 SEATS_BELOW_TAKEN`) or below the stored launch minimum (`409 SEATS_BELOW_LAUNCH_MINIMUM`; both in one request and in disagreement is a `422`). This is checked under the route's row lock, the lock adding a rider takes (M3).
- **Status** (GZ-024):
  - `public`/`student`: `draft` → `forming` (needs round-trip and one-way prices, else `409 ROUTE_NOT_READY`) → `active` ⇄ `paused`; `forming` → `draft` only while nobody is reserved today or later (`409 ROUTE_HAS_RIDERS`, the same rule as a delete's).
  - `corporate`: `draft` → `active` (its company active, else `409 ROUTE_NOT_READY`) ⇄ `paused`.
  - Any other move is `409 INVALID_STATUS_CHANGE`.
  - **Launching** (`forming` → `active`) takes `launch_date`: the 1st of a month from today to a year ahead, as `"YYYY-MM-DD"` (a `422` on `launch_date` otherwise, or if another move sends one). The riders it counts are those reserved from that day or earlier and not gone before it; fewer than the launch minimum is `409 NOT_ENOUGH_RIDERS`. In the same transaction they all become active from the launch day, reservations for a later month become active from their own date, and a reservation that ended before the launch day lapses (ended: it never rode). `reserved` (launch progress) counts the same way for the next launch day.
  - Launches, returns to draft and deletes run under the route's row lock, the lock adding a reservation takes (M3), so the riders they count can't change meanwhile. A corporate route's start share-locks its company, so it and a company deactivation never both succeed.
- **Riders' guarantees:** corridor, school and company can't change while someone holds a seat today or later (`409 ROUTE_HAS_RIDERS`).
- **Places:** a corridor or company that doesn't exist (or a company that isn't active) is a `422` on its field. Routes share-lock them while referring to them, so a delete or deactivation never crosses a new route.
- **Prices** can't be changed by staff through any endpoint. A test walks every route write in the contract, naming each price field on its own, requires every call to be refused, and checks no route gained or lost a price. A malformed prices body is a `422` before the route is looked at.
- **Audit:** `route.created`, `route.updated`, `route.prices_changed`, `route.areas_changed` (the served areas, before and after), `route.status_changed`, `route.launched` (with the launch date, riders started and reservations lapsed), `route.deleted`, by value (routes hold no personal data).
- **"Today"** is Addis Ababa's date (UTC+3 all year, `core/schedule.py`).

## Route requests

Unmet demand from individual riders (GZ-027): there is no waitlist. Staff record requests from phone calls; the public app will too (M5). Company demand is handled with the company directly.

| Endpoint | Who | What it does |
| --- | --- | --- |
| `POST /v1/route-requests` (`route_requests_create`) | staff | Record a request, `new`. Requires `Idempotency-Key`. |
| `GET /v1/route-requests` (`route_requests_list`) | staff | Oldest first (the order free seats are offered in); filters `status`, `kind`, `open`, `route_id` (asked for or matched to). |
| `GET /v1/route-requests/summary` (`route_requests_summary`) | staff | Where the demand is: open requests per area pair and per route, next to free seats next month. |
| `GET /v1/route-requests/{request_id}` (`route_requests_get`) | staff | One request. |
| `PATCH /v1/route-requests/{request_id}` (`route_requests_update`) | staff | Correct who and when: rider and contact names, phone, school, preferred times, notes. |
| `PUT /v1/route-requests/{request_id}/status` (`route_requests_set_status`) | staff | Match, fulfil or decline. |
| `DELETE /v1/route-requests/{request_id}` (`route_requests_delete`) | admin | Soft delete, for a mistaken record. `204`. |

- **Two kinds:**
  - `new_areas`: from A to B, each a listed area (`from_area_id`, `to_area_id`) or one not listed yet (`from_area_name`, `to_area_name`), A and B different.
  - `route`: a specific running route (`active` or `paused`), usually full.
- **Who:** `rider_type` `adult` or `student`; `rider_name` is who will ride (a student's own name), `contact_name` and `contact_phone` who to call (a parent for a student). A student names their school; a `route` request's route must fit the rider: public for adults, student of the same school (ignoring case) for students. A route or area that doesn't exist or doesn't fit is a `422` on its field.
- **One open request** per phone, kind, route or area pair (names ignoring case) and rider (`409 ROUTE_REQUEST_EXISTS`); siblings are separate requests. A partial unique index enforces it, so two at once record one.
- **Status:** `new` → `matched` (`matched_route_id`: a live route for this rider, any status, since a van to come may still be a draft) → `fulfilled`; `new` → `fulfilled`; `new` or `matched` → `declined` (`decline_reason`). A matched request may be matched to another route. `fulfilled` and `declined` are final (`409 INVALID_STATUS_CHANGE`); the status a request has changes nothing. Open means `new` or `matched`.
- **What a request asks for doesn't change**: to ask for something else, decline it and record a new one.
- **Summary:** area pairs count open `new_areas` requests not matched yet (a name not listed yet counts ignoring case and shows as first recorded); routes count each open request once, against the route it was matched to, else the one it asks for. Busiest first, at most 100 of each, with `*_truncated` when there were more.
- **What open requests name stays:** an area or route an open request names can't be deleted (`409 AREA_IN_USE`, `409 ROUTE_HAS_REQUESTS`). Requests share-lock them while being recorded or matched.
- **Personal data:** rider and contact names, phone, school, notes and the decline reason are shown as `[redacted]` in the audit log (`route_request.created`, `.updated`, `.status_changed`, `.deleted`); kind, areas, routes, times and status by value. Phones never go in URLs, so the list has no phone filter.

## Audit log

Every create, update or delete by staff is recorded in `gize.audit_log` in the same transaction as the change (`core/audit.py`):

```python
async with session.begin():
    before = audit.snapshot(STAFF_AUDIT, staff)
    staff.role = StaffRole.ADMIN
    audit.record(
        session,
        actor_id=caller.id,
        action="staff.role_changed",
        entity=STAFF_AUDIT,
        entity_id=staff.id,
        before=before,
        after=audit.snapshot(STAFF_AUDIT, staff),
    )
```

- **Same transaction:** `record` refuses to run outside one. A failed change leaves no row, and a saved change always has one.
- **Minimised:** each entity (an `AuditedEntity`, e.g. `STAFF_AUDIT`) declares the fields it records and which are shown by value.
  - The rest (names, emails, phones, children's data) appear as `"[redacted]"`, which shows that they changed but never what to. An empty value stays `null`, which shows only whether the field is set.
  - Before and after must hold every declared field (use `snapshot`). An update keeps only the fields that changed; one that changed nothing isn't recorded.
- **Who:** `actor_id` is the staff member. An operator command passes `actor_id=None, operator=True`, so a forgotten actor fails instead of passing as an operator.
- **Append-only, enforced by the database** (migration 0004):
  - the app role can read, and insert only the content columns (the ID and time always come from the database, so an entry can't be backdated);
  - UPDATE and DELETE are revoked; restrictive row-level policies make them affect no rows even if granted again by mistake.
- **Rollback:** the migration's downgrade refuses while the table has rows. Back it up and empty it as the owner first, deliberately.
- **`GET /v1/audit-log`** (admins only):
  - newest first, cursor-paginated (`AuditEntryPage`);
  - filtered by an entity (`entity_type` and `entity_id` together) and/or `actor_staff_id`;
  - `actor_staff_id` is null for operator commands such as the first-admin bootstrap.

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

The command reads the user from Supabase (it must exist, have a confirmed email, and not be banned, deleted or anonymous). It then makes them an active admin in one transaction, and refuses if any admin already exists, active or not. It holds the lock every staff change takes, so two runs at once can't both succeed, nor can a run and an admin's change in the portal interleave. It prints only the user ID, never names or emails. It records the new admin in the audit log (action `staff.first_admin_created`, no actor) and reports it to Sentry (a `first_admin_created` security event).

Exit codes:
- `0`: done.
- `1`: refused, another staff change in progress, or Supabase or the database unavailable. If the database dropped mid-run, the change may or may not have been saved; running the command again is safe, since it refuses once an admin exists.
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
- **Breaking changes fail CI**, compared with the PR's base:
  - `oasdiff breaking --fail-on WARN`: removing or renaming anything, making a field or parameter required, changing a type, tightening a limit;
  - `scripts/contract_codes.py`: removing an error code, or dropping one from an operation. oasdiff can't see these (`x-error-codes` is our own extension).

  New response enum values (an error code, a role) are allowed by `docs/api-contract.md`, but oasdiff 1.33 counts them as breaking. `.github/oasdiff-levels.txt` lowers that one check to info; it can't hold comments, oasdiff reads every line.

  Follow the deprecation steps in `docs/api-contract.md`. Only the owner adds the `breaking-change-approved` label to let one through. The approval sticks to the PR: after pushing more commits, remove and re-add the label so it covers what was added. "Re-run jobs" uses the labels from the original run.
- **Releases are automatic.** When a merge to `main` changes `openapi.json`, `.github/workflows/contract-release.yml` tags `api-vX.Y.Z` and attaches the file to a GitHub release:
  - minor for any change oasdiff reports (an added endpoint, field or enum value) or an added error code;
  - patch for descriptions and summaries only;
  - major for a breaking change, and only if the merged PR carries `breaking-change-approved`. Otherwise the release fails for a human to review and tag by hand.

  A re-run of an old release run does nothing once a newer tag exists. The frontend syncs from these tags. The first release is `api-v1.0.0`.

## Working on a ticket

Follow [`docs/workflow.md`](docs/workflow.md): one ticket, one branch (`feat/GZ-xxx-short-name`), one PR titled `GZ-xxx: <title>` using the PR template. Never push to `main` directly.
