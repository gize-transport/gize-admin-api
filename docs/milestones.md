# Milestones and tickets

Each milestone is one logically related feature. Each ticket becomes **one branch and one PR** titled `GZ-xxx: <title>`. Statuses: `todo` → `in progress` → `in review` → `done`. Update the status line when you start, open the PR and merge. New work discovered along the way becomes a new ticket (next free number in that milestone) – never silent scope growth.

Every ticket's acceptance criteria implicitly include `docs/engineering-standards.md` and the testing rules there.

---

## M0 – Foundation

### GZ-001 · Repository scaffold and tooling
Status: done · Depends on: –
- `pyproject.toml` with uv, ruff, mypy `--strict`, pytest, pytest-asyncio, coverage (gate 85% / services 90%), src layout `src/gize_api`.
- pre-commit: ruff, ruff-format, mypy, gitleaks. `.env.example`, `.gitignore`, `README.md` (setup, commands).
- `docker-compose.yml` with Postgres (same major version as Supabase) and Redis.
- Multi-stage `Dockerfile`, non-root user, `api` and `worker` commands.
- Acceptance: fresh clone → `uv sync`, `docker compose up -d`, `uv run pytest` passes with a placeholder test.

### GZ-002 · CI pipeline
Status: done · Depends on: GZ-001
- GitHub Actions on PRs and `main`: ruff, ruff format check, mypy, pytest with Postgres + Redis services, coverage gate, `pip-audit`, gitleaks, Docker build.
- Migration check job: `alembic upgrade head → downgrade -1 → upgrade head` – moved to GZ-006 (M0 plan decision: no placeholder job that passes without checking anything).
- Acceptance: a PR with a lint error, type error, failing test or committed fake secret fails CI.

### GZ-003 · Settings, app factory and lifecycle
Status: done · Depends on: GZ-001
- `core/config.py` (pydantic-settings), fails fast on missing required settings; no secrets in defaults.
- App factory, lifespan (open/close pools), graceful shutdown (30 s), CORS allowlist per environment, 1 MB body limit, `nosniff` header.
- Acceptance: app refuses to start without required env; oversized body → 413 problem+json; disallowed origin gets no CORS headers.

### GZ-004 · Errors and request IDs
Status: done · Depends on: GZ-003
- RFC 9457 problem+json for all errors, including validation (field + reason only) and unhandled exceptions (generic message).
- Central registry of stable error codes. `X-Request-ID` middleware (accept a safe incoming value or generate one), returned on every response.
- Acceptance: an unhandled exception returns 500 with code + request ID and no internals; the same request ID appears in the log line.

### GZ-005 · Logging, telemetry and Sentry
Status: in review · Depends on: GZ-004
- structlog JSON with request ID, staff ID, path, status, duration; phone masking processor; deny-list for sensitive keys.
- OpenTelemetry (FastAPI, SQLAlchemy, Redis, httpx instrumentation; OTLP exporter configurable; off locally). Sentry with PII scrubbing.
- Acceptance: a test asserts tokens, OTP codes and full phone numbers never appear in captured logs.

### GZ-006 · Database foundation
Status: todo · Depends on: GZ-003
- Async engine via Supabase pooler with connect timeout 3 s and statement timeout 5 s; session dependency.
- SQL bootstrap script (run once by a human, documented in README): schema `gize`, roles `gize_app` (DML only) and `gize_migrator` (owner), grants, revoke from `anon`/`authenticated`.
- Alembic configured to run as `gize_migrator`; base model mixins (UUID PK, timestamps, `deleted_at`); helper that enables deny-all RLS on every new table.
- Test fixtures: real Postgres, migrations applied once, transaction rollback per test, factories.
- CI migration check job (moved from GZ-002): `alembic upgrade head → downgrade -1 → upgrade head`.
- OpenTelemetry SQLAlchemy instrumentation on the engine via `Telemetry` (never the global provider; statements without parameters), plus per-dependency metrics for Postgres (handed over from GZ-005).
- Acceptance: `gize_app` cannot create or drop tables (test); `anon` cannot read `gize` tables (test).

### GZ-007 · Health endpoints
Status: todo · Depends on: GZ-006
- `/health/live` (no dependencies) and `/health/ready` (Postgres + Redis within timeouts).
- Acceptance: ready returns 503 with Redis stopped; live still 200; no versions or internals exposed.

### GZ-008 · Shared building blocks
Status: todo · Depends on: GZ-006
- Cursor pagination (`Page[T]`, encode/decode, max 100, stable order).
- Idempotency: `idempotency_keys` table, dependency for POST handlers, conflict on same key + different body, Arq cleanup job (24 h).
- `core/http.py` outbound client: timeouts, host allowlist, private-IP blocking.
- Arq worker skeleton with graceful shutdown.
- OpenTelemetry httpx instrumentation on the outbound client only (URLs without query strings, no trace headers to third parties), plus per-host request/error/duration metrics (handed over from GZ-005). Sentry already sends no trace headers and keeps no breadcrumbs.
- Acceptance: tests for replayed idempotent request, mismatched body (422), blocked private-IP request, worker job executes in tests.

### GZ-009 · Rate limiting core
Status: todo · Depends on: GZ-008
- slowapi + Redis; key = staff ID, fallback `CF-Connecting-IP` / socket IP; named limit policies; 429 + `Retry-After` problem+json.
- Fail-open (staff) vs fail-closed (OTP/SMS/public) policy when Redis is down; security event logged on limit hits.
- Redis metrics per command: request count, error count, duration (moved from GZ-005; instrument the limiter's own Redis client per client, like the app's).
- `RateLimitExceeded` needs its own handler that sets `Retry-After` (from GZ-004 review). Every security event also becomes a Sentry warning (GZ-005): decide which limit hits are sampled or aggregated so a flood cannot exhaust the Sentry quota.
- Acceptance: tests for limit hit, Retry-After present, both Redis-down behaviours.

---

## M1 – Authentication, staff and contract publishing

### GZ-010 · Supabase token verification
Status: todo · Depends on: GZ-004, GZ-006
- Verify access tokens against the Supabase project's signing method (confirm JWKS vs shared secret for the project and document it); check `exp`, `aud`, `iss`; JWKS cached with timeout and refresh on unknown key.
- Acceptance: expired, wrong-audience, tampered and unsigned tokens rejected (401) and logged as security events.

### GZ-011 · Staff accounts and role guards
Status: todo · Depends on: GZ-010
- `staff` table (id = Supabase user id, role `admin`/`staff`, is_active). `current_staff` dependency; `require_role` guard; documented pattern for object-level checks in services.
- `GET /v1/staff/me` returns the caller's id, name, email and role (used by the admin portal after sign-in).
- Acceptance: valid token for an unknown or inactive staff → 403; role taken from our table even if the token claims otherwise.

### GZ-012 · Audit log
Status: todo · Depends on: GZ-011
- Append-only `audit_log`; `audit.record()` used inside service transactions; PII-minimised before/after.
- `GET /v1/audit-log` (admin only, filters by entity and actor, cursor-paginated).
- Acceptance: a failed transaction leaves no audit row; staff role gets 403.

### GZ-013 · Staff management (admin)
Status: todo · Depends on: GZ-012
- Admin: invite staff (Supabase Admin API via allowlisted client), list, change role, deactivate. An admin cannot deactivate or demote themselves if they are the last active admin.
- Acceptance: idempotent invite; last-admin protection test; all actions audited.

### GZ-014 · Bootstrap first admin
Status: todo · Depends on: GZ-011
- CLI command to promote an existing Supabase user to the first admin; refuses if an admin already exists.

### GZ-015 · Publish the API contract
Status: todo · Depends on: GZ-011
- `scripts/export_openapi.py` writes a deterministic `openapi.json` (sorted keys) at the repo root; committed.
- CI: regenerate and fail if `openapi.json` differs (API changed without updating the contract); `oasdiff breaking` against `main`'s `openapi.json` fails the PR on breaking changes unless the PR has the `breaking-change-approved` label.
- On merge to `main` when `openapi.json` changed: a workflow tags `api-vX.Y.Z` (minor for additive changes, patch otherwise) and attaches `openapi.json` to a GitHub release.
- Every operation has a stable `operationId` (`<domain>_<action>`, e.g. `routes_list`), summary, response schemas and documented error codes; error codes appear as an enum in the schema.
- Replace FastAPI's default 422 (`HTTPValidationError`, `application/json`) with the real `ValidationProblem` (`application/problem+json`) from `core/errors.py`, and document `Problem` for the other error responses (handed over from GZ-004).
- Acceptance: removing a response field in a test PR fails CI; an additive change produces a new minor tag after merge.

---

## M2 – Routes

### GZ-020 · Corridors
Status: todo · Depends on: GZ-012
- CRUD for corridors (name, origin area, destination area, waitlist threshold). Staff edit; admin soft-delete.

### GZ-021 · Routes – create, read, list, update
Status: todo · Depends on: GZ-020
- Route type, code (unique), corridor, morning/return times, seats, status `draft`/`active`/`paused`. Filters (type, status, corridor), cursor pagination, seats taken/available computed without N+1.

### GZ-022 · Route prices (admin only)
Status: todo · Depends on: GZ-021
- Separate endpoint for prices (round trip, one-way, Saturday add-on – Saturday only for `public` routes). Staff cannot change prices via any endpoint (test every route endpoint).

### GZ-023 · Pickup points
Status: todo · Depends on: GZ-021
- Add, edit, remove, reorder pickup points (unique order per route, times ascending for morning run).

### GZ-024 · Route status and deletion
Status: todo · Depends on: GZ-021
- Activate/pause (staff); soft delete (admin) blocked while riders are assigned.

---

## M3 – Riders and registration

### GZ-030 · SMS integration
Status: todo · Depends on: GZ-008
- `SmsSender` protocol, `FakeSmsSender`, Arq `send_sms` job with de-duplication key and retries with backoff; real gateway adapter left as a stub until the provider is chosen.

### GZ-031 · Phone verification (public)
Status: todo · Depends on: GZ-009, GZ-030
- `POST /v1/verification/request` and `/verify`; 6-digit codes stored hashed, 10-minute expiry, 5 attempts; returns a short-lived signed "verified phone" token. Limits: 3 SMS/hour/phone, 10/hour/IP, fail closed.

### GZ-032 · Public registration – adult rider
Status: todo · Depends on: GZ-031, GZ-021
- Requires verified-phone token and Idempotency-Key. Creates rider with requested route (status `new`) or a corridor waitlist entry when no route fits. Limits 5/hour/IP, 3/day/phone.

### GZ-033 · Public registration – parent and student
Status: todo · Depends on: GZ-032
- Guardian + one or more children (school, grade level elementary/high school, home area). Students can only request `student` routes.

### GZ-034 · Rider management (staff)
Status: todo · Depends on: GZ-032
- List/search/filter riders (type, status, route), read detail, update, soft delete (admin). Guardian–child integrity enforced.

### GZ-035 · Assign rider to route
Status: todo · Depends on: GZ-034, GZ-023
- Assign/move a rider to a route + pickup point with plan and Saturday add-on; route row locked; never exceeds seats; rule checks (student ↔ student route, adult ↔ public route, corporate ↔ that company's route).
- Acceptance: parallel test – two assignments for the last seat → exactly one succeeds (`ROUTE_FULL` for the other).

---

## M4 – Waitlists

### GZ-040 · Waitlist management
Status: todo · Depends on: GZ-032, GZ-020
- List waitlists per corridor with counts vs threshold; list entries (paginated); withdraw/remove entries; "ready to open" flag.

### GZ-041 · Open a route from a waitlist
Status: todo · Depends on: GZ-040, GZ-035
- Idempotent action: creates or activates a route for the corridor, assigns waiting riders in order up to the seat count in one transaction, enqueues SMS notifications after commit.
- Acceptance: double submission with the same Idempotency-Key opens once; SMS failure does not roll back assignments.

---

## M5 – Drivers

### GZ-050 · Drivers and vehicles
Status: todo · Depends on: GZ-012
- Staff create/list/read/update driver applications and vehicles (plate unique, seats range). Status `applied`.

### GZ-051 · Driver documents
Status: todo · Depends on: GZ-050
- Signed upload URLs to Supabase Storage; server verifies content type (JPEG/PNG/WebP/PDF) and size ≤ 5 MB; short-lived signed download URLs for staff.

### GZ-052 · Inspection and approval
Status: todo · Depends on: GZ-051
- Record inspection (pass/fail, notes); state machine `applied → approved | rejected`, `approved → inactive`; approval requires a passed inspection and required documents.

### GZ-053 · Assign driver to route
Status: todo · Depends on: GZ-052, GZ-021
- Only approved drivers with a passed inspection and enough seats; one active route per driver per time slot.

---

## M6 – Release readiness

### GZ-060 · Authorization matrix test
Status: todo · Depends on: M1–M5
- One test enumerates every registered route and asserts: auth required unless on the explicit public list; staff vs admin permissions match `product-brief.md`.

### GZ-061 · OpenAPI completeness pass
Status: todo · Depends on: GZ-060
- Every operation has examples for success and each documented error; descriptions reviewed for the frontend team; no undocumented error codes (test compares raised codes with the schema enum).

### GZ-062 · Staging deployment and runbook
Status: todo · Depends on: GZ-061
- Deploy API + worker to staging; run migrations as `gize_migrator`; `RUNBOOK.md` (deploy, rollback, rotate secrets, restore backup, what alerts mean).

### GZ-064 · Overview summary endpoint
Status: todo · Depends on: GZ-041, GZ-052
- `GET /v1/overview` (staff): counts of active routes, riders, waitlists ready to open, driver applications awaiting review; one bounded query each, no N+1.

### GZ-065 · Automated dependency and image updates
Status: todo · Depends on: GZ-002
- Found in GZ-001 review: base images, Compose services, the Dockerfile frontend and pre-commit hooks are pinned by digest/SHA, and `uv.lock` pins Python packages, but nothing proposes updates, so security patches go stale silently.
- Dependabot (or Renovate) for `uv.lock`, Docker digests (`Dockerfile`, `docker-compose.yml`), GitHub Actions and pre-commit hooks; weekly, grouped, each update a normal PR through CI.
- Acceptance: a stale pin produces an update PR that passes CI; nothing auto-merges.

### GZ-066 · Ship logs and route alerts
Status: todo · Depends on: GZ-062 (hosting chosen)
- Found in GZ-005 review: the JSON logs go to stderr and nothing ships them yet, so security-event alerts rely on Sentry warning events (`log_security_event`).
- Ship stderr JSON logs to Grafana Cloud (Loki) with the host's log driver or an agent; alert rules for `category=security`, 5xx rate and `observability_disabled` to the Telegram group and email.
- Acceptance: a forced security event and a forced 500 each reach the Telegram group within 5 minutes.

### GZ-063 · Load and failure drill
Status: todo · Depends on: GZ-062
- Light load test at expected peak (staff + registration traffic); drills: Redis down, DB slow, SMS down – confirm behaviour and alerts.
