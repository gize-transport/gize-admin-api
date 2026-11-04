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
Status: done · Depends on: GZ-004
- structlog JSON with request ID, staff ID, path, status, duration; phone masking processor; deny-list for sensitive keys.
- OpenTelemetry (FastAPI, SQLAlchemy, Redis, httpx instrumentation; OTLP exporter configurable; off locally). Sentry with PII scrubbing.
- Acceptance: a test asserts tokens, OTP codes and full phone numbers never appear in captured logs.

### GZ-006 · Database foundation
Status: done · Depends on: GZ-003
- Async engine via Supabase pooler with connect timeout 3 s and statement timeout 5 s; session dependency.
- SQL bootstrap script (run once by a human, documented in README): schema `gize`, roles `gize_app` (DML only) and `gize_migrator` (owner), grants, revoke from `anon`/`authenticated`.
- Alembic configured to run as `gize_migrator`; base model mixins (UUID PK, timestamps, `deleted_at`); helper that enables deny-all RLS on every new table.
- Test fixtures: real Postgres, migrations applied once, transaction rollback per test, factories.
- CI migration check job (moved from GZ-002): `alembic upgrade head → downgrade -1 → upgrade head`.
- `create_async_engine(..., hide_parameters=True)` so statement parameters never reach error text (from GZ-005 review).
- OpenTelemetry SQLAlchemy instrumentation on the engine via `Telemetry` (never the global provider; statements without parameters), plus per-dependency metrics for Postgres (handed over from GZ-005).
- Acceptance: `gize_app` cannot create or drop tables (test); `anon` cannot read `gize` tables (test).
- Review follow-ups done here (owner-approved): `DATABASE_SSL_ROOT_CERT` with `ssl=verify-full` required in production; `DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW` (5/5) and a documented connection budget; session-mode pooler only; bootstrap drift checks; a separate compose test server.

### GZ-007 · Health endpoints
Status: done · Depends on: GZ-006
- `/health/live` (no dependencies) and `/health/ready` (Postgres within its timeout; Redis checked, logged and alerted on, but never fails readiness: see the Health rule in `engineering-standards.md`).
- Settle the session pattern for handlers and dependencies (from GZ-006 review): a dependency that reads (e.g. auth) autobegins a transaction, after which a service's `session.begin()` raises. Decide one rule (e.g. services use `session.begin()` only on a fresh session, or dependencies read in their own short transaction) and test it with `app_with_db_session`.
- Acceptance: ready returns 503 with Postgres stopped; with Redis stopped, ready stays 200 and Redis being down is logged and counted; live is 200 in both cases; no versions or internals exposed. (Changed from "ready returns 503 with Redis stopped" by owner decision at planning: a shared Redis outage must not take the whole API offline.)

### GZ-008 · Shared building blocks
Status: done · Depends on: GZ-006
- Cursor pagination (`Page[T]`, encode/decode, max 100, stable order).
- Idempotency: `idempotency_keys` table, dependency for POST handlers, conflict on same key + different body, Arq cleanup job (24 h).
- `core/http.py` outbound client: timeouts, host allowlist, private-IP blocking.
- Arq worker skeleton with graceful shutdown.
- Never hold a database transaction across an outbound call (SMS, HTTP): `gize_app`'s idle-in-transaction timeout (10 s) equals the outbound timeout, so the connection would be killed mid-request (from GZ-006 review). Commit first, then call, or enqueue the call as a job.
- OpenTelemetry httpx instrumentation on the outbound client only (URLs without query strings, no trace headers to third parties), plus per-host request/error/duration metrics (handed over from GZ-005). Sentry already sends no trace headers and keeps no breadcrumbs.
- Acceptance: tests for replayed idempotent request, mismatched body (422), blocked private-IP request, worker job executes in tests.
- Decisions at planning: no trace headers to third parties (standard updated); our own outbound span and metric instead of the httpx instrumentation package; 428 for a missing key; redis-py pinned below 6 for Arq.
- Hand-off to M2 (first job enqueued by the API, SMS): pass W3C trace context to the worker in the job (our own process); consider an outbox if a job must never be lost between commit and enqueue.

### GZ-009 · Rate limiting core
Status: done · Depends on: GZ-008
- `limits` (async) + Redis (owner decision at planning: slowapi calls Redis synchronously); key = staff ID, fallback client IP from `CLIENT_IP_HEADER` (new setting) or the socket; named limit policies; 429 + `Retry-After` problem+json.
- Fail-open (staff) vs fail-closed (OTP/SMS/public) policy when Redis is down; security event logged on limit hits.
- Redis metrics per command: request count, error count, duration (moved from GZ-005; instrument the limiter's own Redis client per client, like the app's).
- `RateLimitExceeded` needs its own handler that sets `Retry-After` (from GZ-004 review). Every security event also becomes a Sentry warning (GZ-005): decide which limit hits are sampled or aggregated so a flood cannot exhaust the Sentry quota.
- Acceptance: tests for limit hit, Retry-After present, both Redis-down behaviours.
- `/health/live` and `/health/ready` stay outside the limits (load-balancer probes; readiness already serves one cached result per second), from GZ-007.
- Hand-off to GZ-010 (auth): the auth dependency calls `set_staff_id()`, and staff routes use `rate_limited(STAFF, after=<auth dependency>)` (FastAPI runs decorator dependencies before parameter ones), so limits count per staff member, not per office IP (`rate_limited` refuses a staff policy without `after=`). The limiter runs after auth, so requests rejected by auth are never counted: throttle failed token checks too (e.g. a per-IP policy before auth), or a flood of bad tokens is unlimited and each is a Sentry security event.
- Hand-off to the public sign-up and SMS tickets: check phone limits in the service with `rate_limiter.check(PUBLIC_SIGNUP | SMS, phone=...)`, passing the number normalised to E.164.
- Hand-off to hosting (when chosen): if `CLIENT_IP_HEADER` is set, the API must accept connections from the proxy only (firewall, or Cloudflare authenticated origin pulls). Alert on `db.client.operation.duration{db.system.name="redis", outcome="error"}` (GZ-066).

---

## M1 – Authentication, staff and contract publishing

### GZ-010 · Supabase token verification
Status: done · Depends on: GZ-004, GZ-006
- Verify access tokens against the Supabase project's signing method (confirm JWKS vs shared secret for the project and document it); check `exp`, `aud`, `iss`; JWKS cached with timeout and refresh on unknown key.
- Acceptance: expired, wrong-audience, tampered and unsigned tokens rejected (401) and logged as security events.
- Decisions at M1 planning: asymmetric signing keys only (JWKS; no shared secret in the API), PyJWT with `cryptography`, new setting `SUPABASE_URL`. Failed tokens are throttled per IP (`TOKEN_FAILURES`, the GZ-009 hand-off).

### GZ-011 · Staff accounts and role guards
Status: done · Depends on: GZ-010
- `staff` table (id = Supabase user id, role `admin`/`staff`, is_active). `current_staff` dependency; `require_role` guard; documented pattern for object-level checks in services.
- `GET /v1/staff/me` returns the caller's id, name, email and role (used by the admin portal after sign-in).
- Acceptance: valid token for an unknown or inactive staff → 403; role taken from our table even if the token claims otherwise.
- Built: `gize.staff` (migration 0003; email lower-case and unique; role and non-blank checks), `current_staff` → `CurrentStaff(id, role)`, `require_role`, `staff_router` (STAFF limit), a test that every served route is guarded unless its method and path are listed as public, `GET /v1/staff/me` (`operationId` `staff_me`). Refusals of non-staff and deactivated staff count toward `TOKEN_FAILURES`. Hand-offs: GZ-013 stores emails trimmed and lower-case (the table refuses anything else) and re-reads the caller and target rows inside its own block (deactivation and last-admin checks); owner: turn off public sign-ups in Supabase.

### GZ-012 · Audit log
Status: done · Depends on: GZ-011
- Append-only `audit_log`; `audit.record()` used inside service transactions; PII-minimised before/after.
- `GET /v1/audit-log` (admin only, filters by entity and actor, cursor-paginated).
- Acceptance: a failed transaction leaves no audit row; staff role gets 403.
- Built: `gize.audit_log` (migration 0004; the app role may SELECT and INSERT the content columns only, so IDs and times come from the database; UPDATE/DELETE revoked and refused by restrictive policies; the downgrade refuses while rows exist; checks on action, entity type and that a change was recorded; indexes for newest-first lists overall, per entity and per actor), `core/audit.py` (`record`, `snapshot`, `AuditedEntity`; every declared field required; only changed fields kept; non-visible values `[redacted]`; `operator=True` for actor-less entries), `STAFF_AUDIT` (role and status by value), the first-admin command audited (`staff.first_admin_created`, operator), `GET /v1/audit-log` (`audit_log_list`, admin only, `AuditEntryPage`). Hand-off to GZ-013: audit every staff change with `STAFF_AUDIT`.

### GZ-013 · Staff management (admin)
Status: done · Depends on: GZ-012
- Admin: invite staff (Supabase Admin API via allowlisted client), list, change role, deactivate. An admin cannot deactivate or demote themselves if they are the last active admin.
- Acceptance: idempotent invite; last-admin protection test; all actions audited.
- Built: `POST /v1/staff` (`staff_invite`; Supabase `POST /auth/v1/invite`; `Idempotency-Key`; `STAFF_INVITE` 20/hour per admin), `GET /v1/staff` (`staff_list`, `StaffPage`), `GET /v1/staff/{staff_id}` (`staff_get`), `PATCH /v1/staff/{staff_id}` (`staff_update`; role and/or `is_active`, reactivation included). Codes `STAFF_EMAIL_TAKEN`, `AUTH_ACCOUNT_EXISTS` (a confirmed Supabase account is refused: owner decision), `LAST_ACTIVE_ADMIN`. One advisory lock for every staff change, the first-admin command included (`lock_staff_changes`). Migration 0005 (`first_signed_in_at`, set by a member's first request; `ix_staff_created_at_id`). The last-admin rule counts only active admins who have signed in (owner decision after review: an invited admin may never accept). `idempotency.peek` for calls made before the write transaction. Audit actions `staff.invited`, `staff.role_changed`, `staff.deactivated`, `staff.reactivated`. Owner, before real invites: Supabase Site URL (the portal's set-password page) and custom SMTP.

### GZ-014 · Bootstrap first admin
Status: done · Depends on: GZ-011
- CLI command to promote an existing Supabase user to the first admin; refuses if an admin already exists.
- Built: `python -m gize_api.cli.first_admin --user-id <uid> --name "…"`, new setting `SUPABASE_SECRET_KEY` (optional; `sb_secret_...` only), `integrations/supabase_auth.py` (admin API, `apikey` header). Refuses if any admin exists, active or not; under an advisory lock. The user must have a confirmed email and not be banned, deleted or anonymous. Hand-offs: GZ-012 audits the first-admin creation (done in GZ-012; the `first_admin_created` Sentry event stays as an alert). GZ-013's invites must take `FIRST_ADMIN_LOCK_ID` or handle a unique-constraint error on email or ID.

### GZ-015 · Publish the API contract
Status: done · Depends on: GZ-011
- `scripts/export_openapi.py` writes a deterministic `openapi.json` (sorted keys) at the repo root; committed.
- CI: regenerate and fail if `openapi.json` differs (API changed without updating the contract); `oasdiff breaking` against `main`'s `openapi.json` fails the PR on breaking changes unless the PR has the `breaking-change-approved` label.
- On merge to `main` when `openapi.json` changed: a workflow tags `api-vX.Y.Z` (minor for additive changes, patch otherwise) and attaches `openapi.json` to a GitHub release.
- Every operation has a stable `operationId` (`<domain>_<action>`, e.g. `routes_list`), summary, response schemas and documented error codes; error codes appear as an enum in the schema.
- Replace FastAPI's default 422 (`HTTPValidationError`, `application/json`) with the real `ValidationProblem` (`application/problem+json`) from `core/errors.py`, and document `Problem` for the other error responses (handed over from GZ-004).
- Acceptance: removing a response field in a test PR fails CI; an additive change produces a new minor tag after merge.
- Built: `core/contract.py` (problem+json error responses with `x-error-codes`; 422/413/500 added where they apply), `problem_responses()`, `scripts/export_openapi.py` (`--check`), CI job "API contract" (freshness, a self-test proving the gate fails on a removed `/v1/staff/me` field and passes an added one, `oasdiff breaking --fail-on WARN` vs the base unless labelled `breaking-change-approved`), `contract-release.yml` + `scripts/contract_version.py` (major for approved breaking, minor for any reported change, patch otherwise; first `api-v1.0.0`). oasdiff 1.33.0 pinned by SHA-256 in both workflows.

---

## M2 – Routes

Two kinds of customer (product brief, GZ-025): individual riders prepay monthly on `public` and `student` routes, which collect sign-ups while `forming`; companies are billed afterwards for their own `corporate` routes. There is no waitlist: unmet demand is a route request. Order: GZ-025, 026, 020, 029, 021, 022, 023, 024 (report to the owner), 027, 028.

### GZ-025 · Brief and tickets for the two customer types
Status: done · Depends on: –
- Docs only: the product brief, `architecture.md` (data model, schedule times), `api-contract.md` (schedule times local `HH:MM`), `engineering-standards.md` and these tickets, for prepaid individuals and postpaid companies, forming routes, route requests, freed seats and "add a van". Replaces the corridor waitlist (owner decisions, 2026-10-30).

### GZ-026 · Areas
Status: done · Depends on: GZ-012
- Staff manage the list of areas (neighbourhoods, landmarks): create, list (search by name), read, rename. Name unique ignoring case among live areas (`AREA_NAME_TAKEN`). Admin soft-delete, refused while something uses it (`AREA_IN_USE`). Each later ticket that adds a use of areas (GZ-020 corridors, GZ-023 areas served, GZ-027 requests, M3 riders) extends the check.
- Built: `gize.areas` (migration 0006; name trimmed, unique ignoring case among live areas via `uq_areas_name` on `lower(name)`; soft delete), `POST`/`GET /v1/areas`, `GET`/`PATCH`/`DELETE /v1/areas/{area_id}` (`areas_*`), `AREA_NAME_TAKEN`, audit `area.created`/`renamed`/`deleted`. Shared pieces: `core/text.py` (`ShortText`), `core/db.violated_unique_constraint` (the race behind a pre-check), and `committed_api` moved to tests/conftest.py. No in-use check yet: nothing refers to areas until GZ-020.

### GZ-020 · Corridors
Status: done · Depends on: GZ-026
- A corridor is a pair of areas (from, to; different), each pair once among live corridors (`CORRIDOR_EXISTS`). Staff create, list, read, edit; admin soft-delete (refusing it while routes use the corridor is GZ-021's `CORRIDOR_IN_USE`, as nothing else refers to corridors). No threshold.
- Built: `gize.corridors` (migration 0007; FKs to areas `RESTRICT`; `ck_corridors_ends`; `uq_corridors_areas` on the pair among live corridors; soft delete), `corridors_*` endpoints with area names in one query, `CORRIDOR_EXISTS`, `AREA_IN_USE` (deleting an area a live corridor uses). Share locks on areas while a corridor refers to them; an update lock to delete one. Shared: `core/updates.PartialUpdate` (staff's PATCH uses it too), `core/errors.invalid_field`. `CORRIDOR_IN_USE` comes with GZ-021, when routes use corridors.

### GZ-029 · Companies
Status: done · Depends on: GZ-012
- Company accounts: name (unique among live companies), contact person (name, phone E.164, email: personal data, redacted in the audit log, never logged), billing `postpaid`, notes, active/inactive. Staff create, list, read, edit and deactivate (refused while one of its routes is active or paused: `COMPANY_IN_USE`). Admin soft-delete, refused while it has a route that is not deleted (GZ-021 adds the route checks).
- Built: `gize.companies` (migration 0008; name unique ignoring case among live companies; phone E.164 and email lower-case, checked by the table; billing `postpaid`; notes optional; soft delete), `companies_*` endpoints, `COMPANY_NAME_TAKEN`, audit with contact details and notes redacted, activation as its own entries. Shared: `core/contact.py` (`Email`, `EthiopianMobile`: Ethiopian mobiles typed any usual way, stored `+2519…`/`+2517…`), `core/text.Notes` and `SearchText`, `core/db.escape_like`. Contact name, phone and email are all required (email for postpaid invoices later). `COMPANY_IN_USE` comes with GZ-021.

### GZ-021 · Routes – create, read, list, update
Status: done · Depends on: GZ-020, GZ-029
- Code (unique among live routes, `ROUTE_CODE_TAKEN`), corridor, type, school name (`student`, required) or company (`corporate`, required, active), morning and return times (local `HH:MM`), seats (1–60), launch minimum (`public`/`student`: 1 to seats; none for `corporate`). Created as `draft`. Status and prices are not set here.
- Creates `route_assignments` with id, route_id, start_date, end_date, status (`reserved`/`active`/`ended`); M3 adds the rider, plan and Saturday add-on. Seats are counted per day: seats taken on a day = assignments covering it. Responses show seats taken and free for this month and next, and launch progress ("9 of 12"), computed without N+1. Seats never below seats taken on any day from today (`SEATS_BELOW_TAKEN`, route row locked), nor below the launch minimum (`SEATS_BELOW_LAUNCH_MINIMUM`). Corridor, type, school and company cannot change while riders are on the route (`ROUTE_HAS_RIDERS`). Deleting a corridor a live route uses is refused (`CORRIDOR_IN_USE`, added here: GZ-020 left it for this ticket).
- Filters (type, status, corridor, company), cursor pagination.
- Owner decision (M2 report, 2026-10-31; follow-up PR on this ticket): corridor and school are frozen once the route has prices (`ROUTE_HAS_PRICES`) or open route requests name it (`ROUTE_HAS_REQUESTS`), for everyone; a school's letter case may still be fixed (schools match ignoring case).
- Built: `gize.routes` and `gize.route_assignments` (migration 0009; checks for type-dependent fields, times, seats, launch minimum, prices and corporate status; `uq_routes_code` among live routes), `routes_create`/`list`/`get`/`update`, seat counts per day (busiest day of this month from today, and of next month, two queries per page: `routes.repository.seat_counts`), `ROUTE_CODE_TAKEN`, `SEATS_BELOW_TAKEN`, `SEATS_BELOW_LAUNCH_MINIMUM`, `ROUTE_HAS_RIDERS`, `CORRIDOR_IN_USE` (delete, and changing its areas: owner decision), `COMPANY_IN_USE` (deactivate while a route runs, delete while it has one: GZ-029's hand-off). Type fixed at creation (owner decision). Shared: `core/schedule.py` (`LocalTime` "HH:MM", `addis_today`), audit stores times of day.

### GZ-022 · Route prices (admin only)
Status: done · Depends on: GZ-021
- Separate endpoint for prices (round trip, one-way, optional Saturday add-on – Saturday only for `public` routes; whole birr 1–100,000). Refused for `corporate` routes. Staff cannot change prices via any endpoint (test every route endpoint); GZ-028's copy keeps the original's prices unchanged.
- Built: `PUT /v1/routes/{route_id}/prices` (`routes_set_prices`, admins; `RoutePricesSet`, all prices at once; a missing or null Saturday price is none), `ROUTE_HAS_NO_PRICES` (corporate), Saturday on a non-public route a 422 on `saturday`, audit `route.prices_changed`, and a test that walks every route write in the contract as staff and finds no price changed. No migration: the price columns came with GZ-021.
- For M3: today an admin may clear a public route's Saturday price at any time. Once riders can hold the Saturday add-on, decide whether clearing it is refused while someone holds it, or ends their add-on at the month's end. Decided (GZ-032): refused while someone holds it (`409 ROUTE_HAS_RIDERS`).

### GZ-023 · Route times and areas served
Status: done · Depends on: GZ-021, GZ-026
- A route is A to B (owner decision, 2026-10-31; replaces pickup points with their own times, never merged). Arrival times: `morning_arrival_time` (at B) and `return_arrival_time` (at A), required on every route, in order: leave A < arrive at B < leave B < arrive at A. A breaking change to GZ-021's released contract, approved by the owner (`breaking-change-approved`; nothing used it yet).
- Areas served: the areas a route passes between A and B, in order, no times (`PUT /v1/routes/{route_id}/areas`, the whole list; staff). Not A or B themselves; each once; live areas; at most 20. Shown on every route as `served_areas`, without N+1. An area a live route serves can't be deleted (`AREA_IN_USE`).
- The 5-minute boarding wait at A and arrival allowance at B are fixed rules, recorded in the brief, checked once trips are recorded (later).
- For M3: if a rider's boarding area is recorded, setting a route's areas must refuse to drop one a rider boards in.
- Built: migration 0010 (`routes.morning_arrival_time`/`return_arrival_time`, nullable in the table until a contract step after deploys, `ck_routes_arrival_times`; `route_areas` with `uq_route_areas_route_id_position`/`_area_id`); `RouteCreate` requires both arrival times; `RouteUpdate` changes them, the four in order; `PUT /v1/routes/{route_id}/areas` (`routes_set_areas`, `RouteAreasSet`, staff), `served_areas` on every route (one extra query per page), audit `route.areas_changed`; a route serving areas keeps its corridor until its list is cleared (422 on `corridor_id`); an older route without arrival times must get both at once; `AREA_IN_USE` for areas live routes serve. The pickup-point design (stops with times) was dropped before merging.

### GZ-024 · Route status and deletion
Status: done · Depends on: GZ-022, GZ-023
- `public`/`student`: `draft` → `forming` (round-trip and one-way prices, else `ROUTE_NOT_READY`) → `active` (staff choose the launch date, the 1st of a month not before today; riders reserved for that month ≥ launch minimum, else `NOT_ENOUGH_RIDERS`; reservations become active from that date in the same transaction, earlier ones moved to it, later ones active from their own start date) ⇄ `paused`; `forming` → `draft` only while nobody is reserved. `corporate`: `draft` → `active` (an active company) ⇄ `paused`. Other moves: `INVALID_STATUS_CHANGE`. Repeating a move changes nothing. Staff.
- Admin soft delete, refused while a rider is reserved or active (`ROUTE_HAS_RIDERS`).
- From GZ-021's review: a status change locks the route, then share-locks a corporate route's company and requires it active (a deactivation racing an activation must not both commit); launch progress and the launch count use one rule (`routes.repository.next_launch_day`: riders reserved from that day or earlier).
- After this ticket: report to the owner (routes working end to end; launching is tested with reservations made by test factories, since sign-ups come in M3) before GZ-027.
- Built: `PUT /v1/routes/{route_id}/status` (`routes_set_status`, staff; `RouteStatusSet`, `launch_date` a strict `"YYYY-MM-DD"`: `core/schedule.LocalDate`), `DELETE /v1/routes/{route_id}` (`routes_delete`, admins), `ROUTE_NOT_READY` (also a corporate route whose company isn't active), `NOT_ENOUGH_RIDERS`, `INVALID_STATUS_CHANGE`, `ROUTE_HAS_RIDERS` for forming → draft and delete (one rule: reserved or riding today or later); `launch_date` at most a year ahead (a bound on the input); audit `route.status_changed`, `route.launched` (launch date, riders started, lapsed), `route.deleted`. One rule for launch counting and progress (`routes.repository._reserved_for`): reserved from the launch day or earlier and not ended before it; a reservation that ended before the launch day lapses (ends) at the launch. No migration.
- For M3: reservations are expected to end (status `ended`) when a rider cancels before a launch; the lapse rule only covers one left `reserved` with an end date before the launch day. Any change to an assignment (a cancellation, an end date), not only an insert, takes the route's lock first (`get_live_route(for_update=True)`): a launch counts reservations, then starts them, under that lock, and taking the assignment's row lock first would deadlock against it.
- Owner decision (2026-10-31, GZ-024 report): a launch dated a later month makes the route `active` at once, riders starting on the launch day; it does not stay `forming` until then. Anyone new meanwhile is a route request, offered a free seat in order.

### GZ-027 · Route requests (staff-recorded)
Status: done · Depends on: GZ-024
- Individual demand: `new_areas` (from/to areas, or free text for an area not listed yet) or `route` (a specific running route, usually full). Rider type (adult/student), school, preferred times, contact name and phone (personal data), notes, source. Statuses: `new` → `matched` (to a route) → `fulfilled`; `new` → `fulfilled` directly; `new` or `matched` → `declined` (reason). Open = `new` or `matched`. A duplicate open request (same phone, kind, route or area pair, and rider name) is refused (`ROUTE_REQUEST_EXISTS`); siblings are separate requests. Admin soft-delete.
- Summary: open requests per area pair and per route, next to each route's free seats next month; bounded.
- Built: `gize.route_requests` (migration 0011; checks per kind, rider type and status; `uq_route_requests_open`, partial on live open rows), `/v1/route-requests`: create (Idempotency-Key), list (oldest first; `status`, `kind`, `open`, `route_id`), `summary`, get, PATCH (who and when only: what is asked for doesn't change), `PUT .../status`, admin delete. `rider_name` next to the contact (the duplicate rule needs it; siblings share a parent's phone). A `route` request needs a running route that fits the rider (public for adults; student, same school ignoring case, for students); a match any live route that fits. Areas and routes an open request names can't be deleted (`AREA_IN_USE`, new `ROUTE_HAS_REQUESTS`); requests share-lock them (`routes.repository.refer_to_live_route`). Personal data redacted in the audit log. No phone filter: phones never go in URLs. Lock order (architecture.md): routes before requests; `set_status` share-locks the matched route, then locks the request.
- For GZ-040: lock the route, then its requests (the order above). Owner decision (M2 report): a route's school and corridor can't change while open requests name it (`ROUTE_HAS_REQUESTS`).

### GZ-028 · Add a van
Status: done · Depends on: GZ-024
- Copy a route: corridor, type, school or company, times, prices (unchanged: staff may copy, not change, them), areas served; new code, own seats and launch minimum, `copied_from_route_id`. An individual route's copy starts `forming` if the original meets the forming requirements, otherwise `draft`; a corporate copy starts `draft`. Idempotency-Key.
- From GZ-024's review: a route may be deleted after being copied, so a copy's `copied_from_route_id` can name a deleted route (a 404 if followed); show it as such rather than failing.
- Built: `POST /v1/routes/{route_id}/copies` (`routes_copy`, staff, Idempotency-Key; `RouteCopy {code, seats, launch_minimum?}`: prices can't be named, the price walk covers it). The original is share-locked (`refer_to_live_route`), so its areas and prices are copied whole; a corporate original's company is share-locked and must be active (`409 ROUTE_NOT_READY`). Audit `route.copied` (and `route.areas_changed` for the copied areas). Copies of copies work; a deleted original stays named. No migration. The price walk now sends each write that creates a route a valid body with the price merged in.
- Owner decisions (2026-10-31, M2 report): a route's corridor and school are frozen once it has prices or open route requests, for everyone (`409 ROUTE_HAS_PRICES`, `409 ROUTE_HAS_REQUESTS`, checked under the route's lock; GZ-021 follow-up): to serve somewhere else, add a route. Copies of routes made before arrival times existed keep none, until staff set both. The request summary's area pairs count only requests not matched yet.

### GZ-067 · Route readiness for the admin frontend
Status: done · Depends on: GZ-024
- Asked by the frontend (2026-10-31): the UI must not work out the status rules itself. What does a route still need before it can start forming, and when can it launch for a given month?
- Built: every route (list, read and every write's response) has `status_changes`: the moves its type allows from its status, each with `ready`, `blocked_by` (`RouteBlocker`: `PRICES_MISSING`, `COMPANY_INACTIVE`, `ROUTE_HAS_RIDERS`, `NOT_ENOUGH_RIDERS`) and, for a launch, the earliest `launch_date` it is judged for. Same checks as `PUT /status`; a test walks every listed change through `PUT /status`. `GET /v1/routes/{route_id}/launch-months` (`routes_launch_months`, staff): every 1st a launch may name (12, or 13 on the 1st), riders reserved for each, `ready` only on a forming route at its minimum; one query. Whether anyone holds a seat comes from the existing reserved-count query, so a page still takes four. Both are snapshots: a change can still be refused if something moves meanwhile. No migration.

### GZ-068 · Local sample data for the frontend
Status: done · Depends on: GZ-067
- Asked by the owner (2026-10-31): the frontend works against the API on the same Mac before staging, and needs data to show.
- Built: `python -m gize_api.cli.sample_data`, only with `APP_ENV=local` and a `DATABASE_URL` on this machine (exit 2 otherwise, before connecting), refuses a database with any areas, companies, routes or route requests (even deleted). It loads through the services as the first active admin, so rules and audit entries are the API's: 9 areas, 4 corridors, 3 companies (one inactive), 8 routes in every status reachable without riders, 6 route requests. Each step commits on its own; a failed run means rebuilding the local database (README). It loads no riders or seats: rider-less seats would block M3's migrations, so riders are added in M3 through its own flows. Its phones look like real mobiles (the API accepts nothing else): GZ-030's fake sender must stay the local default. README: "Sample data" and "Running the API for the frontend".

---

## M3 – Riders and registration

### GZ-030 · SMS integration
Status: done · Depends on: GZ-008
- `SmsSender` protocol, `FakeSmsSender`, Arq `send_sms` job with de-duplication key and retries with backoff; real gateway adapter left as a stub until the provider is chosen.
- Owner decisions (2026-11-04): Render's free plan for about six months, so no worker: SMS is sent inline by the request that needs it. No provider yet: a fake locally and in tests; staging and production have no sender (every send fails as `unavailable`, never silently dropped).
- Built: `integrations/sms.py` (`SmsSender`, `SmsSendError` kinds unavailable/rejected/timeout, `FakeSmsSender` keeping the latest 100 and, locally, printing each message with the number masked, `UnconfiguredSmsSender`, `sms_sender_for(settings)`); `sms/service.send_once`: claims the event's key in `gize.sms_messages` (insert on conflict do nothing: a double request sends one SMS), sends with no transaction open, records `sent` or `failed` (an unexpected error is recorded as `error`, then raised). No automatic retry: a timed-out POST may have been delivered. The text is never stored. Migration 0012 (expand only, RLS).
- From review: refuses to run inside a database transaction (`OpenTransactionError`); a send cancelled midway is recorded as `timeout`; every call is measured (`gize.sms.send.duration`: purpose, outcome) and a failure is a `sms_failed` warning, never with the phone or text, so staging's missing provider shows. A row left `sending` (process died, or the record failed after the send) is final for its key: GZ-031 must treat `sending` like `failed` (ask again with a new key, counted against the per-phone limit).
- Still to come: the gateway adapter (through `core/http.py`) once the provider is chosen; purging old `sms_messages` rows (they hold phone numbers) with the other cleanup when a worker or scheduled job exists; the Arq job when the plan has a worker.

### GZ-031 · Phone verification (public)
Status: done · Depends on: GZ-009, GZ-030
- `POST /v1/verification/request` and `/verify`; 6-digit codes stored hashed, 10-minute expiry, 5 attempts; returns a short-lived signed "verified phone" token. Limits: 3 SMS/hour/phone, 10/hour/IP, fail closed.
- Owner decisions (M3 plan, 2026-11-04): a new secret `VERIFIED_PHONE_TOKEN_KEY` (optional: unset, verification answers 503 and sends nothing; nothing set on Render yet); English SMS text for now ("Gize: your code is 123456. It expires in 10 minutes.").
- Built: `gize.phone_verifications` (migration 0013: id chosen before insert, phone, `code_hash` = HMAC under a key derived from the setting, `expires_at`, `attempts` 0–5, `verified_at`; RLS). Request: `SMS` limits (route: per IP; service: per phone), then in one transaction claim the Idempotency-Key (scoped by a hash of the phone), store, record the 201; then GZ-030's `send_once` (`otp:<verification id>`, via `core.db.borrowed` on the request's session); if not `sent` (failed, or left `sending`), `idempotency.release` the key, expire the verification, 503 `SMS_UNAVAILABLE` with Retry-After. Verify: `VERIFICATION_CHECK` (30/hour per IP, fail closed); the row locked `FOR UPDATE`; expired/used/exhausted/unknown are one 410 `VERIFICATION_EXPIRED`; a wrong code commits its attempt, then 422 `VERIFICATION_CODE_INVALID`; the right one sets `verified_at` and returns an HS256 token (issuer `gize-api`, audience `gize-verified-phone`, `sub` phone, `jti` verification, 30 minutes) signed with a second derived key. The first public routes: listed in `tests/staff/test_auth.py` and `tests/test_authorization.py`.
- From review: anything that stops the send after the commit (a database failure, a bug, cancellation) also releases the key and expires the code, so a stored "sent" never outlives an SMS that wasn't; the idempotency scope is an HMAC of the phone under a third derived key (a plain hash of a phone number is reversible, and scopes are logged); the key is checked before the limits count. Replays count against the limits (documented); `/verify` is not idempotent (a lost answer means a new code).
- For GZ-032: a sign-up reads the token (`read_verified_phone_token`) and spends it once, by its verification (add `consumed_at`). Before public sign-up goes live: `CLIENT_IP_HEADER` on Render (shared IP otherwise), an SMS provider, CORS for the rider app, and purging old verification rows.

### GZ-036 · Public catalogue for the rider app
Status: done · Depends on: GZ-023, GZ-024
- Found while planning GZ-032 (owner decision, 2026-11-04): the rider app can't choose areas or a route without public reads, and no ticket had them. Comes before GZ-032.
- `GET /v1/public/areas` (live areas, `id` and `name`; `q` finds names starting with it) and `GET /v1/public/routes?from_area_id=&to_area_id=` (both required, different): `public` routes that are `forming`, `active` or `paused`, have both prices, and serve the trip (boarding at A or an area served, getting off at B). Each with its code, A and B, areas served, the four times, prices, status and `full` (no free seat from the next 1st, the earliest a new rider starts), never seat counts, launch progress or anything staff-only. Paginated. Public, `PUBLIC_READ` limit (60/minute per caller; fails open: read-only and cheap). Student routes join in GZ-033 (by school).
- Built: `catalogue/` (`public_areas_list`, `public_routes_list`), `routes.repository.serves_trip` (GZ-032 reuses it) and `public_routes`, `ratelimit.PUBLIC_READ`; listed in both guard tests. No migration.
- From review: `full` is judged from the next 1st (today on the 1st), the earliest a new rider starts, not "this month or next"; the limit is shown to be per caller. Before launch: mobile carriers put many phones behind one address, so watch the rate of 429s on these reads once `CLIENT_IP_HEADER` is set.

### GZ-032 · Public registration – adult rider
Status: done · Depends on: GZ-031, GZ-024, GZ-027
- Requires verified-phone token and Idempotency-Key. Creates the rider and either a reservation on a chosen `forming` `public` route (from the 1st of a month; seats checked under the route row lock: the shared assignment service and its parallel last-seat test are built here, and GZ-035 reuses them) or a route request (kind `route` for an active route, full or not; `new_areas` when no route serves their areas). It never gives a place on an active route directly: freed seats go to the oldest request (GZ-040). Inserting a seat takes the route row lock first (`get_live_route(for_update=True)`), then inserts: taking it after the insert's foreign-key lock could deadlock two sign-ups. Limits 5/hour/IP, 3/day/phone.
- Owner decisions (M3 plan, 2026-11-04): one live adult rider per phone (`409 RIDER_EXISTS`; changes go through staff, GZ-034); no start month to choose: a reservation is held from the next 1st and the launch decides the month; public reads came first (GZ-036).
- Built: `POST /v1/registrations/adult` (`registrations_adult`; public, `PUBLIC_SIGNUP`: route per IP, service per phone; Idempotency-Key scoped by the keyed phone hash). Migration 0014 (expand): `gize.riders` (adult columns, `uq_riders_adult_phone`, RLS), `route_assignments.rider_id` (`uq_route_assignments_rider_id_held`: one held seat per rider), `route_requests.rider_id`, `phone_verifications.consumed_at`. One transaction: claim, spend the token (verification locked: once), lock the route, share-lock the areas, create the rider, then `routes.seats.reserve` (the shared seat service: counts from the next 1st under the route lock, `409 ROUTE_FULL`) or `route_requests.service.add_from_app` (kind `route` for active/paused, `new_areas` without a route; `ROUTE_REQUEST_EXISTS` as for staff). A refusal rolls back everything, token included. The plan and Saturday add-on live on the rider (a request needs them too); Saturday needs a route offering it (422 `saturday`), and is kept as a wish without a route. New codes: `401 PHONE_NOT_VERIFIED`, `409 RIDER_EXISTS`, `409 ROUTE_FULL`. Areas a live rider uses can't be deleted (`AREA_IN_USE`). Public sign-ups aren't audited (the log records staff changes); rows say `source = app`.
- From review: without a route, a `new_areas` request only when no route would take them (a running one, or a forming one with a seat: 422 `route_id`); forming routes all full count as none (demand for a van). A request staff recorded from a call (same phone, areas or route, name) is linked to the rider (`rider_id`) and keeps its place, instead of a 409 that would block the sign-up. A retry with the key replays before the per-phone count and the token's expiry (the per-caller count still includes it). Areas are share-locked in a fixed order. The race tests hold the contended row lock until both sign-ups wait on it. GZ-023 and GZ-022's hand-offs are now live, so: staff can't drop an area a rider holding a seat boards in, and admins can't clear the Saturday price while such a rider has the add-on (`409 ROUTE_HAS_RIDERS`).
- Owner decision (2026-11-04, GZ-032 report): clearing a route's Saturday price stays refused while a rider holding a seat has the add-on; it is not ended at the month's end. GZ-022's open question is settled.
- For GZ-035: the rider's home area is where they board.

### GZ-033 · Public registration – parent and student
Status: todo · Depends on: GZ-032
- Guardian + one or more children (school, grade level elementary/high school, home area). Students can only sign up for, or request, `student` routes of their school.

### GZ-034 · Rider management (staff)
Status: todo · Depends on: GZ-032
- List/search/filter riders (type, status, route), read detail, update, soft delete (admin). Guardian–child integrity enforced.

### GZ-035 · Assign rider to route
Status: todo · Depends on: GZ-034, GZ-023, GZ-032
- Assign/move a rider to a route (boarding at A or a served area) with plan and Saturday add-on; route row locked; never exceeds seats on any day; rule checks (student ↔ student route, adult ↔ public route, corporate ↔ that company's route). Individual riders: a `forming` route gives a reservation, an `active` one a place, starting on the 1st of a month; company riders (recorded by Gize staff) start on any day. Placing an individual rider on an active route while that route has older open requests needs a skip reason, audited (the brief's skip rule).
- A rider leaves a route: individual riders at the end of a month (the seat stays empty until the next month), company riders on any day.
- Acceptance: parallel test – two assignments for the last seat → exactly one succeeds (`ROUTE_FULL` for the other, who can be recorded as a route request); uses GZ-032's assignment service, extended to staff assignment and company riders.
- The parallel test needs committed data on two connections: `db_session` runs inside one rolled-back transaction, so add a committed-data fixture that truncates its tables afterwards (from GZ-006 review).

---

## M4 – Requests and launches

### GZ-040 · Public route requests and seat offers
Status: todo · Depends on: GZ-031, GZ-027, GZ-035
- Public route request (verified-phone token, Idempotency-Key, PUBLIC_SIGNUP limits). Offer a free seat on a running route (freed or never filled, from the next month) or a place on a forming route to open requests by SMS, oldest request first (staff may skip with a reason, audited); an offer expires after the agreed time (open question in the brief, 48 hours proposed) and passes to the next request. An accepted offer becomes a reservation or place.
- Acceptance: two acceptances for one seat → exactly one succeeds; an expired offer cannot be accepted.

### GZ-041 · Launch notifications
Status: todo · Depends on: GZ-024, GZ-030, GZ-035
- When a route launches (GZ-024), every rider on it gets an SMS, enqueued after the commit; an SMS failure never undoes the launch. Idempotent: a repeated launch sends nothing more.

### GZ-042 · Company days without service
Status: todo · Depends on: GZ-029, GZ-024
- Staff record dates a company's route does not run (date, reason, who asked); listed per route and company, for drivers and later postpaid billing. Notice rule and charging basis to confirm with the owner first.

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

### GZ-069 · Admins correct staff names
Status: done · Depends on: GZ-013
- Found setting up staging (2026-11-02): the first admin's name came from the command line as a placeholder ("Your Name"), and nothing could change it. Owner decision: admins may correct any staff member's name, their own included; emails stay fixed (the Supabase sign-in).
- Built: `full_name` on `StaffUpdate` (`PATCH /v1/staff/{staff_id}`, admins), the same rules as an invite's name; audit `staff.renamed` (names redacted), one entry per kind of change; never a security event or a log line; `LAST_ACTIVE_ADMIN` never applies to a name. Contract: one optional request field (minor).

### GZ-060 · Authorization matrix test
Status: done (first pass, brought forward before staging) · Depends on: M1–M5
- One test enumerates every registered route and asserts: auth required unless on the explicit public list; staff vs admin permissions match `product-brief.md`.
- Owner decision (2026-11-02): run it before the staging deploy (GZ-062), so nothing reaches the internet unchecked; M3–M5 endpoints are added to the matrix in their own PRs (the first test fails until they are).
- Built: `tests/test_authorization.py`. `ACCESS` names every endpoint served outside local and staging with `admin`, `staff` or `public`; a test requires it to match the app exactly. Each endpoint is then called with no token (401 `UNAUTHENTICATED`), as a signed-in non-staff user and a deactivated admin (403 `FORBIDDEN`), as staff (403 on admin endpoints, never 401/403 otherwise) and as an admin. Mutation-checked: loosening a delete, tightening a status move and opening the audit log each fail it.

### GZ-061 · OpenAPI completeness pass
Status: todo · Depends on: GZ-060
- Every operation has examples for success and each documented error; descriptions reviewed for the frontend team; no undocumented error codes (test compares raised codes with the schema enum).

### GZ-062 · Staging deployment and runbook
Status: done (brought forward before M3; GZ-061 stays where it is) · Depends on: GZ-061
- Deploy API + worker to staging; run migrations as `gize_migrator`; `RUNBOOK.md` (deploy, rollback, rotate secrets, restore backup, what alerts mean).
- Owner decisions (2026-11-02): host Render, free plan, for staging; the existing Supabase project (`pahktmbnbyulqkhdruci`, eu-west-1) becomes staging; production later on a paid plan with its own Supabase project; the frontend isn't deployed yet (no CORS origin).
- Built: `render.yaml` (Blueprint: `gize-api-staging`, Docker, free, Frankfurt, `/health/ready`, auto-deploy off, secrets `sync: false`; `gize-redis-staging` Key Value, private, `noeviction`); `.github/workflows/deploy-staging.yml` (after CI passes on `main`: `alembic upgrade head` as `gize_migrator`, then the deploy hook for that commit; one at a time, never cancelled); Supabase's public root CA committed (`docker/certs/`, pinned by SHA-256, verified against the pooler with `verify-full`) and copied into the image, so no host secret files are needed; `.env.staging.example` for the one-off bootstrap and first admin; `RUNBOOK.md`; `tests/test_deploy_config.py`.
- Free plan, so not yet: the worker (only purges idempotency keys until M3; no free workers), Render's pre-deploy step (paid; the workflow migrates instead), backups (Supabase free has none; RUNBOOK says how to take a copy). `CLIENT_IP_HEADER` stays empty: Render documents no trustworthy client-IP header; settle it before M3's public routes.
- Found on the first Render deploy (2026-11-02): `COPY --chmod=0444` also set the mode of the `/app/certs` directory on Render's builder (not on local BuildKit), so the app user couldn't open the CA (`PermissionError` at startup). Fixed with a plain `COPY` (root-owned, 0644, directory 0755); CI's image job now checks that uid 10001 can read the file and can't write it or the directory.
- Done when: the owner has followed RUNBOOK "First staging setup", a merge to `main` deploys by itself, and `/health/ready` and a signed-in `/v1/staff/me` answer on staging.
- Live (2026-11-02): `https://gize-api-staging.onrender.com`. The owner followed the first setup (bootstrap, first migration, Blueprint, GitHub secrets, first admin). From outside: `/health/live` and `/health/ready` 200, unauthenticated and forged-token requests 401, `/docs` served, no CORS for unknown origins. The deploy workflow ran end to end on `26014af` (tip check, migrations as `gize_migrator` with verify-full, deploy hook) and staging stayed ready throughout. Still to see: a signed-in `/v1/staff/me` on staging, once the frontend points at it.
- Found during setup: a value pasted into Render with extra characters fails startup with a `database_url` format error (the value itself is never logged). RUNBOOK's copy command avoids it. The first admin's name on staging is the placeholder "Your Name"; staff names can't be changed through the API yet (follow-up).

### GZ-064 · Overview summary endpoint
Status: todo · Depends on: GZ-040, GZ-041, GZ-052
- `GET /v1/overview` (staff): counts of active routes, riders, forming routes ready to launch, open route requests, driver applications awaiting review; one bounded query each, no N+1.

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
