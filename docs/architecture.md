# Architecture

## Components

```
 Admin portal (Next.js, own repo) ─┐
 Rider registration app ───────────┼──HTTPS──▶  Cloudflare (proxy, WAF) ──▶  Gize Admin API (FastAPI, this repo)
 (later: rider/driver apps)       ─┘                                              │        │          │
                                                                                  ▼        ▼          ▼
                                                                   Supabase Postgres   Redis      External (allowlisted)
                                                                   (schema "gize")    (limits,    SMS gateway, Supabase
                                                                   Supabase Auth JWKS  Arq jobs)  Storage, Sentry, OTel
                                                                                          │
                                                                                   Arq worker (same image)
```

- **This API is the only writer.** No client talks to the `gize` schema directly. Supabase's auto-generated Data API must not expose `gize`.
- **Supabase Auth** issues login tokens (staff in the admin portal). The API verifies them; it never issues its own passwords.
- **Arq worker** runs from the same Docker image with a different command (SMS sending, notifications, cleanup jobs).

## Repositories

- `gize-admin-api` (this repo) – FastAPI, worker, migrations, tests.
- `gize-admin-web` – Next.js admin portal. Consumes this API through a TypeScript client generated from our OpenAPI schema. Never shares code by copy-paste.

## Layers (strict, one direction)

```
api (routers)  →  services (business rules)  →  repositories (data access)  →  models (SQLAlchemy)
      │                    │
   schemas (Pydantic in/out)   core (settings, security, errors, logging, telemetry, rate limits)
```

- **Routers**: HTTP only – parse input (schemas), call one service function, return a schema. No business logic, no queries.
- **Services**: all business rules and authorization decisions (role + object ownership). Own transactions (`async with session.begin()`).
- **Repositories**: SQLAlchemy queries only. Every list method takes a bounded page size and a cursor.
- **Models**: SQLAlchemy 2.0 typed mappings in schema `gize`.
- **Schemas**: Pydantic v2 with `model_config = ConfigDict(extra="forbid")` and explicit length/size constraints. Separate `Create`, `Update` and `Read` schemas; never return ORM objects or internal fields.
- Domain modules are vertical slices: `areas/`, `companies/`, `routes/`, `route_requests/`, `riders/`, `drivers/`, `staff/`, each with `router.py`, `schemas.py`, `service.py`, `repository.py`, `models.py`.

## Folder layout

```
src/gize_api/
  main.py                 # app factory, middleware, routers, lifespan (startup/shutdown)
  models.py               # imports every domain models.py, so Alembic sees the whole schema
  worker.py               # Arq worker: jobs, cron, settings; `python -m gize_api.worker`
  core/
    config.py             # pydantic-settings; all env vars; fails fast on missing secrets
    db.py                 # async engine, session dependency, timeouts, 503 classification
    models.py             # declarative Base (schema gize), UUID/timestamp/soft-delete mixins
    rls.py                # enable_row_level_security(op, table) for migrations
    bootstrap.py          # one-off roles/schema/grants script, run by a human
    migrations.py         # migration settings, engine, lock (used by migrations/env.py)
    security.py           # JWT verification (Supabase JWKS), current_staff dependency, role guards
    errors.py             # exception types → RFC 9457 problem+json responses; problem_responses()
    contract.py           # finishes the OpenAPI document into the contract (error responses)
    logging.py            # structlog JSON, request context, PII masking
    telemetry.py          # OpenTelemetry setup (traces, metrics)
    sentry.py             # Sentry init and event scrubbing
    ratelimit.py          # `limits` (async) + Redis: named policies, keys, fail-open/closed
    idempotency.py        # Idempotency-Key handling (Postgres table)
    http.py               # outbound HTTP client with timeouts + host allowlist (SSRF guard)
    pagination.py         # cursor encode/decode, Page[T] schema
    jobs.py               # enqueue background jobs (after commit, de-duplicated by job ID, JSON)
    redis_options.py      # timeouts and TLS name checks for every Redis client
    audit.py              # audit log writer used by services
  health/router.py        # /health/live, /health/ready
  staff/  areas/  companies/  routes/  route_requests/  riders/  drivers/  verification/   # domain slices
  audit_log/              # GET /v1/audit-log (admins); the writer is core/audit.py
  integrations/sms.py     # SmsSender protocol + FakeSmsSender + real gateway adapter later
  integrations/supabase_auth.py  # Supabase Auth admin API (secret key; read users, invites later)
  cli/first_admin.py      # operator command: make the first admin (`python -m gize_api.cli.first_admin`)
migrations/               # Alembic (env.py runs as gize_migrator)
tests/
  conftest.py             # real Postgres (compose test server), per-test transaction rollback, auth helpers
  factories.py            # Factory[Model] test data helpers
  <domain>/test_*.py
docker-compose.yml        # postgres, redis (local; CI runs the same images as job services)
Dockerfile                # multi-stage, non-root user, healthcheck
```

## Authentication and authorization

1. Staff log in to Supabase Auth from the admin portal; the portal sends `Authorization: Bearer <access token>`.
2. `core/security.py` verifies the signature against the project's JWKS (asymmetric ES256/RS256 keys only; the project must use Supabase signing keys, not the legacy shared secret), plus `exp`, `aud`, `iss`, `sub` and `role`. The JWKS is cached for 10 minutes and refreshed on an unknown `kid` at most every 30 s.
3. The token's `sub` is looked up in `gize.staff` by `current_staff`; the account must exist and be active (403 otherwise). Role (`admin` | `staff`) comes **from our table, never from the token**. `require_role(...)` guards actions by role. Staff routers are created with `staff_router(...)`, which adds the per-staff `STAFF` rate limit; a test fails if any served route lacks `current_staff` or that limit and its method and path are not on the public list.
4. Services check the role for the action **and** that the target object exists, is not soft-deleted and may be acted on. Missing or forbidden objects return the same 404 to avoid leaking existence where appropriate. The pattern:
   ```python
   async def update_route(session, caller: CurrentStaff, route_id, changes):
       async with session.begin():
           route = await repository.get_route_for_update(session, route_id)  # not deleted
           if route is None or not may_edit(caller, route):
               raise AppError(ErrorCode.NOT_FOUND)  # same answer for both
           ...
   ```
   Re-read anything the decision depends on inside the service's own block: `current_staff` read the caller moments earlier, in a block that is already closed.
5. Public endpoints (registration, route requests, phone verification) are unauthenticated, rate-limited per IP and per phone, and require a verified phone token from the OTP flow.

Login rate limits for Supabase Auth are configured in the Supabase dashboard (Auth → Rate limits), not in this API.

Staff changes (invites, role and status changes, the first-admin command) all take one transaction-level advisory lock (`staff.repository.lock_staff_changes`), so rules that span rows, such as "at least one active admin", are checked with nothing changing underneath. A service that must call an outside service before writing (a staff invite calls Supabase) does it between two transactions: a short read block that also `peek`s the idempotency key, the call, then the write block that claims the key.

Row locks are taken in one order, so two changes never wait on each other: places (areas, corridors, companies), then routes, then route requests and route assignments. A change that needs a route's lock and a request's takes the route's first (GZ-027 `set_status`; GZ-040's freed seats lock the route, then requests). Within a table, a row lock comes before the joined load (lock-then-load).

## Data model (MVP – refine in the plan for each ticket)

All tables in schema `gize`. UUID primary keys. `created_at`, `updated_at` (UTC, timestamptz) on every table. Soft delete via `deleted_at` on business tables; default queries exclude deleted rows. Money as integer whole birr. Phones stored E.164 (`+2519…` / `+2517…`).

| Table | Key columns |
| --- | --- |
| `staff` | id (= Supabase user id), full_name, email, role (`admin`/`staff`), is_active, first_signed_in_at (null until the first request: an invitation not yet accepted) |
| `audit_log` | id, actor_staff_id, action, entity_type, entity_id, before (jsonb), after (jsonb), request_id, created_at – append-only |
| `idempotency_keys` | key, actor, request_hash, response_status, response_body, created_at (purged after 24 h) |
| `areas` | id, name (unique ignoring case among live areas), deleted_at |
| `corridors` | id, origin_area_id, destination_area_id (each pair once among live corridors), deleted_at |
| `companies` | id, name, contact_name, contact_phone, contact_email, billing (`postpaid`), is_active, notes, deleted_at |
| `routes` | id, code, corridor_id, route_type (`public`/`student`/`corporate`), school_name (student), company_id (corporate), morning_time (leaves A), morning_arrival_time (at B), return_time (leaves B), return_arrival_time (at A) (local `HH:MM`, in that order), seats, launch_minimum (public/student), price_round_trip, price_one_way, price_saturday (public only), status (`draft`/`forming`/`active`/`paused`; corporate never `forming`), copied_from_route_id, driver_id (nullable), deleted_at |
| `route_areas` | id, route_id, area_id, position – the areas a route passes between A and B, in order (no times); replaced as a whole list, rows not soft-deleted (the audit log keeps each list) |
| `guardians` | id, full_name, phone |
| `riders` | id, rider_type (`adult`/`student`/`corporate`), full_name, phone (adults and company riders), guardian_id (students), company_id (company riders), school_name (exact match with the route's, until a schools list exists), grade_level, home_area_id, destination_area_id, status, deleted_at |
| `route_assignments` | id, route_id, start_date, end_date (null while ongoing), status (`reserved` on a forming route / `active` / `ended`) (GZ-021); rider_id, plan, saturday_addon (M3) – individual riders start on the 1st and end on a month's last day, company riders any day; seats are counted per day, enforced with a row lock on `routes`; never deleted (ended instead) |
| `route_requests` | id, kind (`new_areas`/`route`), from_area_id or from_area_name, to_area_id or to_area_name (a name for an area not listed yet), route_id (kind `route`), rider_type (`adult`/`student`), rider_name, school_name, preferred_morning_time, preferred_return_time, contact_name, contact_phone, notes, source (`staff`/`app`), status (`new`/`matched`/`fulfilled`/`declined`; open = `new` or `matched`), matched_route_id, decline_reason, created_at, deleted_at (admin) – seat offers (M4) add their own columns |
| `phone_verifications` | id, phone, code_hash, attempts, expires_at, verified_at |
| `drivers` | id, full_name, phone, is_owner, status (`applied`/`approved`/`rejected`/`inactive`) |
| `vehicles` | id, driver_id, plate, vehicle_type, seats, inspection_status, inspected_at, inspection_notes |
| `driver_documents` | id, driver_id, doc_type, storage_path, content_type, size_bytes, uploaded_at |

## API conventions

- All routes under `/v1/`, except `/health/live` and `/health/ready` (for the load balancer; not in the OpenAPI contract). Plural nouns: `/v1/routes`, `/v1/routes/{id}/areas`.
- JSON, snake_case. Timestamps (`created_at` and the like) are ISO 8601 UTC; the client converts to Africa/Addis_Ababa. Schedule times of day (a route's leave and arrive times) are local Addis Ababa time, `"HH:MM"`: a daily timetable, not a moment (Ethiopia has no daylight saving).
- Errors: RFC 9457 `application/problem+json` with `type`, `title`, `status`, `code` (stable machine code, e.g. `ROUTE_FULL`), `request_id`. Validation errors list field names and reasons only.
- Lists: cursor pagination – `?limit=25&cursor=…` (max 100); response `{ "items": [...], "next_cursor": "…" | null }`. Stable ordering (`created_at, id`).
- `Idempotency-Key` header **required** on POSTs that create or trigger side effects (creating a record, registration, a route request, approve driver, send SMS). Same key + same body → same stored response; same key + different body → 422.
- `ETag`/`If-Match` optional later; for now use `updated_at` checks in services where concurrent edits matter.
- Every response carries `X-Request-ID`.
- OpenAPI is the contract; every endpoint has a summary, documented error codes and response schemas.
- **Session rule** (`core/db.py`):
  - Every use of a database session is inside `async with session.begin():`, one block per unit of work. Sessions are created with autobegin off, so a query outside a block fails at once; `session.add()` does not, so add objects inside the block that should save them.
  - A dependency that reads (auth) uses its own short block, finishes it before the handler's service opens its own, and hands on plain values (IDs, role), not ORM objects. Role and ownership checks belong in the service's block, next to the write they protect.
  - Every ORM query and `session.get()` refreshes rows the session already holds, so a later block never sees stale data (except inside `no_autoflush`, where a refresh would overwrite unsaved edits).
  - Never hold a block open across an outbound call (SMS, HTTP).

## Configuration and environments

| Environment | Database | Redis | Notes |
| --- | --- | --- | --- |
| Local | Postgres in Docker | Redis in Docker | Fake SMS sender, Sentry/OTel off |
| Staging | Second free Supabase project | Managed or container Redis | Real integrations in test mode |
| Production | Main Supabase project | Managed Redis | Host TBD |

All configuration via environment variables loaded by `core/config.py`; app refuses to start if a required secret is missing. `.env.example` lists every variable with a dummy value. Connect through Supabase's session-mode connection pooler, port 5432 (the direct database host is IPv6-only); transaction mode is not supported. Production verifies the database certificate (`ssl=verify-full`, `DATABASE_SSL_ROOT_CERT`).

## Deployment

- One Docker image; commands: `api` (uvicorn with graceful timeout 30 s), `worker` (`python -m gize_api.worker`: Arq with our logging; 20 s to finish running jobs on SIGTERM) and `migrate`.
- Migrations run as a separate release step with the `gize_migrator` credentials, before the new app version starts.
- Host-agnostic: no host-specific SDKs or files in application code.
