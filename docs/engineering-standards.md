# Engineering standards

These apply to every ticket. A PR that breaks one is not done, whatever the tests say. Reviewers (human or Claude) check each section.

## Security

- **Authorization on every handler, at the object level.** Check the caller's role for the action *and* that the specific record may be read or changed by them (exists, not soft-deleted, belongs to the caller where ownership applies – e.g. a guardian's own children). "Is logged in" is never sufficient. Public endpoints are explicitly marked and listed in the PR.
- **Validate all input at the boundary** with Pydantic v2 schemas using `extra="forbid"`. Every string has `max_length` (default 255; longer only when justified), every list has `max_length` (default 100), numbers have ranges, enums are enums. Request bodies capped at 1 MB; uploads 5 MB, images (JPEG/PNG/WebP) or PDF only, checked by content, not just extension.
- **Parameterized queries only** via SQLAlchemy. Never build SQL, shell commands or file paths from strings containing input. Any `text()` query must be justified in the PR and flagged for review.
- **Secrets** come from environment/settings only. Never log, return or hard-code them. `.env` files are git-ignored; gitleaks runs in pre-commit and CI. The Supabase service-role key is used only where strictly needed (Storage signed URLs) and never sent to clients.
- **Outbound requests** go through `core/http.py`, which allows only hosts in a configured allowlist (SMS gateway, Supabase, Sentry, OTel endpoint) and blocks private/loopback/link-local IPs. No feature fetches user-supplied URLs.
- **Errors returned to clients:** generic message + stable error code + request ID. No stack traces, SQL, exception text, internal IDs or library names. Full detail goes to logs/Sentry only.
- **Uploads** are stored in Supabase Storage under random names; clients get short-lived signed URLs; content type and size are verified server-side.
- **Security headers and CORS:** CORS allows only configured origins (admin portal per environment). Responses include `X-Content-Type-Options: nosniff`; no wildcard origins with credentials.
- **Dependencies:** pinned in `uv.lock`; `pip-audit` runs in CI and fails on known vulnerabilities.

## Database

- **Least privilege.** The app connects as `gize_app`: SELECT/INSERT/UPDATE/DELETE on schema `gize` only, no DDL, not superuser, no access to `auth`/`storage` schemas except what is explicitly granted. Alembic runs as `gize_migrator`, which owns the schema.
- **Supabase exposure:** schema `gize` is not exposed through Supabase's Data API. RLS is enabled on every table with no policies for `anon` and `authenticated` (deny-all) as defense in depth. Per-user authorization lives in the services and is tested.
- **Transactions:** multi-step writes run in one transaction owned by the service. Default isolation READ COMMITTED; state in the plan when something needs more. Seat assignment locks the route row (`SELECT … FOR UPDATE`) and re-counts seats inside the transaction, so a route is never overbooked.
- **Every list query is paginated and bounded** (default 25, max 100, cursor-based). Watch for N+1 queries – use `selectinload`/joins deliberately; tests assert query counts for list endpoints. Every new query pattern names the index it uses; add indexes in the same migration.
- **Migrations are backward-compatible with the deployed code (expand/contract).** Add columns nullable or with defaults; backfill in a separate step; remove old columns only in a later release. Every migration has a working `downgrade()` that is tested in CI (upgrade → downgrade → upgrade). No destructive change without a written rollback plan in the PR.
- **Soft delete** (`deleted_at`) for business records; only Admin can delete. Audit log rows are append-only and never deleted by the app.
- **Audit log:** every create/update/delete by staff writes an `audit_log` row in the same transaction (actor, action, entity, before/after with PII-minimised fields, request ID).
- Constraints live in the database too: NOT NULL, CHECK (phone format, positive seats, prices ≥ 0), UNIQUE (route code, plate), foreign keys with explicit `ON DELETE` behaviour.

## Reliability

- **Timeouts on every network call:** Postgres statement timeout 5 s and connect timeout 3 s; Redis 1 s; outbound HTTP 10 s total (connect 3 s). No call without a timeout.
- **Retries** only for idempotent operations, with exponential backoff and jitter, capped attempts. Never retry a non-idempotent write.
- **Idempotency keys:** mutating endpoints that clients may retry require `Idempotency-Key`; stored in `gize.idempotency_keys` for 24 hours (cleanup job in Arq).
- **Graceful shutdown:** on SIGTERM stop accepting new requests, finish in-flight work within 30 s, close DB/Redis pools. Arq worker finishes the current job or re-queues it.
- **Background jobs** are idempotent (safe to run twice) and record outcomes; SMS sends are de-duplicated by job key.
- External services (SMS) sit behind interfaces with a fake for local/tests; failure of a non-critical dependency degrades the feature, not the API.

## Rate limiting and abuse

- Limits via `limits` (async) with Redis storage, keyed by **staff user ID**, falling back to client IP: the header named in `CLIENT_IP_HEADER` (`CF-Connecting-IP` behind Cloudflare), otherwise the socket address – never trust any other forwarded header. (slowapi was replaced at GZ-009: it calls Redis synchronously and would stall every request while Redis is slow.)
- Defaults:
  - Staff endpoints: 120 requests/minute per user.
  - Public registration and waitlist sign-up: 5/hour per IP and 3/day per phone.
  - Phone verification SMS: 3/hour per phone and 10/hour per IP (each SMS costs money). Code entry: 5 attempts per code, then the code is invalidated.
  - Expensive endpoints (exports, bulk actions): per-endpoint limits stated in the ticket plan.
- Return **429** with `Retry-After` and a problem+json body.
- **If Redis is unavailable:** staff endpoints fail **open** (allow, log a warning, raise an alert); OTP, SMS and public registration endpoints fail **closed** (503).
- Staff login limits are configured in Supabase Auth settings (documented in the README setup steps).

## Observability

- **Structured JSON logs** via structlog. Every line has `request_id`, `staff_id` (when authenticated), route/endpoint, status and duration. Never log tokens, passwords, OTP codes, names, children's data or full phone numbers (mask as `+2519••••1234`).
- **Metrics** (OpenTelemetry → Grafana Cloud): request rate, error rate and duration per endpoint; the same for each external dependency (Postgres, Redis, SMS). New endpoints and dependencies must be covered.
- **Traces:** OpenTelemetry tracing for HTTP, SQLAlchemy, Redis and outbound HTTP. W3C trace context goes only to our own processes (the worker); outbound calls to third parties never carry trace headers, which would hand them our internal trace IDs (owner decision, GZ-008 planning).
- **Errors:** Sentry with PII scrubbing on; request ID attached.
- **Security events** – failed token verification, permission denials, rate-limit hits, repeated OTP failures – logged at WARNING with an `event` field; Grafana/Sentry alerts send them to the private Telegram group and email.
- **Health:** `GET /health/live` (process up, no dependencies) and `GET /health/ready`. Neither requires auth; neither reveals versions or internals.
  - **Readiness depends on Postgres only.** It fails (503) when Postgres does not answer within its timeout, which takes the instance out of the load balancer's rotation.
  - **Redis is checked but does not fail readiness.** Every instance shares one Redis, so a Redis outage would otherwise take the whole API offline, defeating the fail-open rule for staff endpoints (see "Rate limiting and abuse"). Redis being down is logged at ERROR, counted in a metric and alerted on; features that need Redis decide per endpoint (staff endpoints fail open; OTP, SMS and public registration fail closed with 503).
  - Owner decision, GZ-007 planning (2026-10-28).

## Testing (required for "done")

- **Every endpoint** has tests for: happy path; unauthenticated; wrong role; wrong owner / soft-deleted object (no IDOR); invalid input (unknown field, too long, wrong type, out of range); dependency failure (DB timeout, Redis down, SMS failure) with the expected behaviour.
- **No mocking the database** for data-access or service logic. Tests run against real Postgres in Docker, using the same migrations, with a transaction rolled back per test. External HTTP (SMS) uses the fake implementation or a recorded stub.
- Concurrency-sensitive logic (seat assignment, opening a route from a waitlist, idempotency) has a test that runs competing requests in parallel.
- Migrations: CI runs `upgrade head → downgrade -1 → upgrade head` on every PR.
- Coverage gate: 85% overall and 90% for `services/` modules; coverage must not drop on a PR.
- Tests are deterministic: no real network, no sleeps for timing, frozen time where time matters.

## API contract

- Follow `docs/api-contract.md`. Any change to request/response shapes, endpoints or error codes regenerates and commits `openapi.json` in the same PR.
- Within `/v1` only additive changes; breaking changes need the deprecation process and a matching frontend ticket.
- Every endpoint has a stable `operationId`, response schemas and its error codes listed.

## Code quality

- mypy `--strict` passes; no `Any` without a comment explaining why. ruff lint + format clean.
- Small functions with clear names; docstrings on services explain the business rule, not the code.
- No commented-out code, no TODOs without a ticket ID.
- Conventional commits referencing the ticket: `feat(routes): add seat lock on assignment [GZ-031]`.
