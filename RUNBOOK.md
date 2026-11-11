# Runbook – Gize Admin API

How to set up, deploy, roll back and look after the hosted API. Staging only for now. Production gets its own section when it is set up: a paid plan, its own Supabase project, observability required.

## Staging at a glance

| Piece | Where | Notes |
| --- | --- | --- |
| API | Render web service `gize-api-staging` (free plan, Frankfurt) | Built from the `Dockerfile`, configured by `render.yaml`. It sleeps after 15 minutes without traffic, so the first request after that takes about a minute. |
| Redis | Render Key Value `gize-redis-staging` (free plan) | Private network only. It loses its data on restart, which only resets rate limits for now. |
| Database and sign-in | Supabase project `pahktmbnbyulqkhdruci` (eu-west-1) | Postgres schema `gize` plus Supabase Auth. The free plan has no automatic backups and pauses after about a week without activity (unpause it in the dashboard). |
| Deploys | `.github/workflows/deploy-staging.yml` | After CI passes on `main`: migrations, then Render's deploy hook for that commit. |
| Admin portal (CORS) | `https://admin.gizetransport.com` | The only browser origin allowed (`CORS_ALLOWED_ORIGINS` in `render.yaml`, 2026-11-10). Add more comma-separated: `https`, no path, no trailing slash. |
| Logs, metrics, traces, alerts | Grafana Cloud stack `maroonstarfish2393` (India) | See "Grafana Cloud" and "Alerts" below. |

Not running on staging yet:
- **The worker:** it only clears old idempotency keys until M3 sends SMS, and Render's free plan has no workers.
- **`CLIENT_IP_HEADER`:** Render documents no trustworthy client-IP header; this must be settled before the public sign-up routes (M3).
- **Sentry:** optional on staging, so the API logs `observability_disabled setting=sentry_dsn` at startup. Grafana is set up (GZ-066).

## Staging secrets

Never put any of these in chat, code, tickets or logs. Each lives in exactly the places listed.

| Secret | Lives in | Used by |
| --- | --- | --- |
| `DATABASE_URL` (`gize_app`) | Render (`gize-api-staging` → Environment); your local `.env.staging` | the API |
| `MIGRATOR_DATABASE_URL` (`gize_migrator`) | GitHub → Settings → Secrets and variables → Actions: `STAGING_MIGRATOR_DATABASE_URL`; `.env.staging` | the deploy workflow's migrations |
| `SUPABASE_SECRET_KEY` | Render; `.env.staging` | staff invites; the first-admin command |
| `VERIFIED_PHONE_TOKEN_KEY` | Render only | phone verification (GZ-031) |
| `AFROMESSAGE_TOKEN` | Render only (AfroMessage's dashboard makes it) | sending SMS (GZ-038) |
| Render deploy hook URL | GitHub: `RENDER_STAGING_DEPLOY_HOOK_URL` | the deploy workflow |
| The `postgres` database password | Supabase; your password manager; `.env.staging` only while bootstrapping | the bootstrap |

## First staging setup (once)

Do the steps in order; each one assumes the ones before it.

1. **Supabase dashboard:**
   - **Project Settings → Data API:** the exposed schemas must not include `gize`. The defaults are `public` and `graphql_public`.
   - **Authentication → Sign In / Providers:** turn off **Allow new users to sign up**. Staff are invited by admins.
   - **Project Settings → JWT Keys:** the project must use asymmetric signing keys, not the legacy shared secret. Otherwise every token is refused with `401`.
2. **Passwords:** choose one for `gize_app` and one for `gize_migrator`. Make them long and use only letters and digits, so they need no escaping in a URL. Keep them in your password manager.
3. **Local file:** `cp .env.staging.example .env.staging`, then fill in the database password, the two new passwords and the Supabase secret key. Git ignores `.env.staging`.
4. **Bootstrap the database:** this creates the roles, the schema and the grants; it's safe to run again.
   ```bash
   uv run --env-file .env.staging python -m gize_api.core.bootstrap
   ```
   Then delete the `BOOTSTRAP_ADMIN_DATABASE_URL` line from `.env.staging`.
5. **First migration:**
   ```bash
   uv run --env-file .env.staging alembic upgrade head
   ```
6. **Render** (copy each secret with `grep '^NAME=' .env.staging | cut -d= -f2- | tr -d '\n' | pbcopy`, so nothing extra is pasted):
   - **New → Blueprint**, pick `gize-transport/gize-admin-api` and accept `render.yaml`.
   - When it asks, paste `DATABASE_URL` and `SUPABASE_SECRET_KEY` from `.env.staging`.
   - It creates `gize-api-staging` and `gize-redis-staging` and builds the first deploy.
7. **GitHub Actions secrets:**
   - `STAGING_MIGRATOR_DATABASE_URL`: from `.env.staging`.
   - `RENDER_STAGING_DEPLOY_HOOK_URL`: `gize-api-staging` → Settings → Deploy Hook.
8. **First admin:** use the same Supabase user as locally, because the project is shared.
   ```bash
   uv run --env-file .env.staging python -m gize_api.cli.first_admin --user-id <UID> --name "Full Name"
   ```
9. **Check:**
   - `https://<service>.onrender.com/health/ready` answers `{"status":"ok"}`. Allow a minute if it was asleep.
   - `/docs` loads.
   - A request with your token to `/v1/staff/me` answers `200`.

## Deploying

Merging to `main` deploys, with nothing to do by hand:
1. CI passes on the merge.
2. **Deploy staging** runs `alembic upgrade head` as `gize_migrator`.
3. It calls the deploy hook with that commit.
4. Render builds the image and switches over once `/health/ready` passes. If the check fails, Render keeps the old version running.

Deploys run one at a time and never cancel each other. A run deploys its commit only while that commit is still the tip of `main`. CI runs can finish out of order, and an old run can be re-run, so an older commit never replaces a newer one: the newer commit's own run deploys it. A skipped run shows a notice saying so.

Migrations are expand/contract (engineering standards), so the version still running works with the new schema. A migration that would break the running version is a bug in that migration, not a deploy problem.

**If the workflow fails:**
- **Before "Migrate":** nothing changed. Fix the cause (often a missing secret, named in the error) and re-run the job from the Actions tab.
- **At "Migrate":** Alembic runs each migration in its own transaction, so the database is at the last migration that succeeded. Read the error, fix it on a branch, and merge again. "Another migration is running" means a second run overlapped; wait for it and re-run.
- **At "Deploy":** the schema is migrated, but the old code still runs, which expand/contract allows. Re-run the job, or press **Manual Deploy → Deploy latest commit** in Render.

Render emails the workspace owner when a deploy fails.

## Rolling back

- **Code:** in Render, open `gize-api-staging` → **Events**, pick the last good deploy and choose **Rollback**. Then revert the bad PR on `main`, so the next deploy doesn't bring it back.
- **Schema:** rarely needed, because the old code runs on the new schema. If a migration itself is wrong, write a new migration that fixes it. Only for a migration that never saw real data, from your machine:
  ```bash
  uv run --env-file .env.staging alembic downgrade -1
  ```
  Every migration's downgrade is tested in CI.

## Rotating secrets

| What | Steps |
| --- | --- |
| `gize_app` or `gize_migrator` password | 1. Put the new password into `.env.staging`, plus a temporary `BOOTSTRAP_ADMIN_DATABASE_URL`. 2. Re-run the bootstrap, which sets the role passwords from the URLs. 3. Update Render (`DATABASE_URL`, which restarts the API) or GitHub (`STAGING_MIGRATOR_DATABASE_URL`). 4. Remove the bootstrap line again. Between steps 2 and 3, the API's new connections fail; keep the gap short. |
| `SUPABASE_SECRET_KEY` | 1. Create a new secret key in Supabase (Project Settings → API Keys). 2. Set it in Render and `.env.staging`. 3. Delete the old key in Supabase. |
| `VERIFIED_PHONE_TOKEN_KEY` | 1. Generate a new one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. 2. Set it in Render. Codes and tokens in flight (10 and 30 minutes) stop working. |
| `AFROMESSAGE_TOKEN` | 1. Generate a new token in AfroMessage's dashboard. 2. Set it in Render. 3. Revoke the old one in AfroMessage. |
| Deploy hook URL | 1. Regenerate it in Render (`gize-api-staging` → Settings). 2. Update `RENDER_STAGING_DEPLOY_HOOK_URL` in GitHub. The old URL stops working at once. |
| The `postgres` database password | Reset it in Supabase (Project Settings → Database). Nothing deployed uses it. |

After any leak, rotate first and investigate second. A leaked `SUPABASE_SECRET_KEY` can create users: check Authentication → Users. A leaked `AFROMESSAGE_TOKEN` spends your balance: check AfroMessage's sent messages.

## Backups and restoring

Supabase's free plan takes **no automatic backups**, so staging data can't be restored unless you took a copy. Before a risky migration on staging, take one from your machine with Postgres 17 client tools.

Run it as `gize_migrator`, which owns everything in `gize`. The bootstrap gives `postgres` no access to the schema, and `pg_dump` uses libpq's TLS options rather than the app's `?ssl=`. So build the URL from `MIGRATOR_DATABASE_URL` in `.env.staging`, replacing `?ssl=verify-full` with the libpq form:

```bash
pg_dump --schema=gize --format=custom --file=staging-$(date +%F).dump \
  "postgresql://gize_migrator.pahktmbnbyulqkhdruci:<password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=verify-full&sslrootcert=docker/certs/supabase-root-2021-ca.crt"
```

**To restore,** stop deploys first: don't merge anything meanwhile. Then run, with the same URL:

```bash
pg_restore --clean --if-exists --single-transaction --dbname="<same URL>" staging-<date>.dump
```

The copy carries the tables' grants to `gize_app` and the Alembic version, so afterwards check two things:
- `uv run --env-file .env.staging alembic current` names the migration the copy was taken at;
- `/health/ready` answers.

Practise this on your local database before you rely on it. For production, choose a paid plan with daily backups or point-in-time recovery before launch.

## Dependency updates

Dependabot (`.github/dependabot.yml`) opens update PRs on Mondays.
- **Grouping:** one PR per ecosystem for minor and patch updates, and a PR of its own for each major. The ecosystems are Python packages, GitHub Actions, the Docker images, the Compose services and the pre-commit hooks.
- **Cooldown:** a release must be 5 days old before it's proposed.
- **Merging:** each one is a normal PR. CI runs, and a person merges it only when every check is green: `gh pr checks <n> --watch`, then `gh pr merge <n> --squash --delete-branch`. Nothing merges by itself.

**Before merging:**
- Read a major's release notes.
- After a runtime package or the Python image deploys, check staging's `/health/ready`.

**Bumps CI fails on purpose:** some versions live in two places, so CI fails until both match. Finish these on the PR's branch, push, and CI runs again.
- **Python image:** set `UV_PYTHON` to the new patch release in every workflow (`ci.yml`, `contract-release.yml`, `deploy-staging.yml`, `pin-check.yml`).
- **uv image:** set `UV_VERSION` in `ci.yml`, `contract-release.yml` and `deploy-staging.yml`. In `pyproject.toml`, set the floor of `required-version` (a floor only: Dependabot runs its own, newer uv, which a ceiling would refuse) and the range of `uv_build`. Then run `uv lock`.
- **gitleaks hook:** set `GITLEAKS_VERSION` and `GITLEAKS_SHA256` in `ci.yml`.
- **Compose services (Postgres, Redis):** copy the new `image:` lines into the `services:` of `ci.yml`, so CI tests what developers run.

**Normal parts of a Python package update:**
- **Raised minimums:** Dependabot's uv updates also raise the minimum versions in `pyproject.toml` (`alembic>=…`).
- **A changed contract:** a FastAPI or Pydantic update can change `openapi.json`. Regenerate it on the branch (`uv run python scripts/export_openapi.py`) and commit it; the contract check still refuses a breaking change.
- **Anything more** (code changes, a breaking contract change) needs a ticket: close the PR.

**The "Stale pins (weekly check)" issue:** the pin-check workflow reports the two pins no Dependabot ecosystem sees. It counts only releases at least 5 days old.
- **oasdiff:** `OASDIFF_VERSION` and `OASDIFF_SHA256` in `ci.yml` and `contract-release.yml`. The checksum is the `linux_amd64` tarball's line in the release's checksums file on GitHub.
- **Dockerfile frontend:** the digest on the first line (`# syntax=docker/dockerfile:1@sha256:…`); the issue shows the new one.
- Bump them on a `chore/deps-<what>` branch, through a normal PR. The issue closes itself on the next Monday run, or run the workflow by hand from the Actions tab.

**Versions Dependabot ignores:** Python 3.13+, Postgres 18+ and Redis 8+. These are decisions, not updates: each needs a ticket.

**Security updates:** they come as soon as an advisory is published, whatever the cooldown, once *Dependabot alerts* and *Dependabot security updates* are on (Settings → Advanced Security).

## What the signals mean

Logs, metrics and traces are in Grafana Cloud (next section); this table is for what you see without it.

| You see | It means | Do |
| --- | --- | --- |
| `/health/ready` not 200, or Render marks the deploy failed | The database or Redis is unreachable from the new version | Check the Supabase status and that the project isn't paused. Check `DATABASE_URL` in Render. Read the Render logs for `database_unavailable` and its SQLSTATE. |
| Requests take about a minute after a quiet spell | The free instance was asleep | Nothing. A paid plan doesn't sleep. |
| `503` with `Retry-After` | The database was busy or briefly down | Retry. If it persists, check Supabase. |
| `401` on every request | The token is from another Supabase project, or the project uses the legacy JWT secret | Check `SUPABASE_URL` and the JWT signing keys. |
| `403` for a signed-in user | They aren't active staff in this database | An admin invites them, or the first-admin step is missing. |
| `observability_disabled` at startup | Sentry or Grafana isn't configured | Expected on staging for Sentry (not set up yet). In production it alerts: GizeApiMisconfigured. |

## Drills

The last drill's results are in `docs/drills/2026-11-11-staging.md`. Repeat it before production, and after any change to timeouts, rate limits, alerts or hosting. Run it on **staging only**, while nobody else is using it.

1. **An admin's token, about an hour** (the seed sets prices and deletes, which are admin-only):
   - `uv run python -m scripts.drill_token` asks for the publishable key, an email and a password, for an account that has a password.
   - **Or copy the portal's token:** reload a data page in the admin portal, then DevTools → Network → an `onrender` request → right-click **authorization** → **Copy value**. Then run:
     `(umask 077; pbpaste | tr -d ' \r\n' | sed 's/^Bearer//' > ~/.gize-drill-token) && pbcopy < /dev/null`

     It's private from the start and the clipboard is cleared afterwards.
2. **Data:** `uv run python -m scripts.drill_seed seed --token-file ~/.gize-drill-token`
   - It takes about 12 minutes. Every ID created goes into `~/.gize-drill-seed.json`.
   - Add `--launch 5 --allow-sms` **only while staging has no AfroMessage token**: launches text their riders, whose made-up numbers are valid-format ones someone may own. The phone-code part of the load (`--signups N --allow-sms`) texts too. Without `--allow-sms`, both are refused.
3. **Load:** `uv run python -m scripts.load_drill --minutes 10 --token-file ~/.gize-drill-token`. Keep away from the portal meanwhile: it shares the account's limit.
4. **Drills, each under a shorter load (`--minutes 4`):**
   - **Database slow:** hold an `ACCESS EXCLUSIVE` lock on `gize.routes` for 60 s as `gize_migrator` (`.env.staging`), in a transaction that rolls back.
   - **Redis down:** suspend `gize-redis-staging` in Render, wait for GizeApiDependencyUnavailable's email (about 2 minutes), then resume it.
   - **SMS down:** staging without an AfroMessage token already is: the launches show it.
   - **Phone codes:** they test registration only once staging has `VERIFIED_PHONE_TOKEN_KEY`. Without it they answer 503 before anything else runs.
5. **Clean up:**
   - `uv run python -m scripts.drill_seed remove --token-file ~/.gize-drill-token` (about 12 minutes; it can run again if interrupted);
   - then `rm ~/.gize-drill-token ~/.gize-drill-seed.json`.

## Demo data (staging)

Staging holds realistic made-up data for partner demos (GZ-074, `scripts/demo_seed.py`), created through the API like real staff work:
- **Places and people:** real Addis areas; made-up Ethiopian names and valid-format phones.
- **Routes:** 3 public routes launched for the next 1st, each with a driver; forming routes, some ready to launch; one draft.
- **Riding today:** a company route with its staff, the only riders who can start before a 1st.
- **Requests:** some waiting for routes, some for new areas.

Its IDs are in `~/.gize-demo-seed.json` on the machine that made it.
- **Remove it:** `uv run python -m scripts.drill_seed remove --token-file ~/.gize-drill-token --state ~/.gize-demo-seed.json` (an admin's token).
- **Make it again:** `uv run python -m scripts.demo_seed seed --token-file ~/.gize-drill-token --allow-sms`.
- **Removing it the day it was made** stops at the company riders, who rode today: remove it a day later. If a `seed` run stops after its riders, `demo_seed finish` completes it.

**Before giving staging an AfroMessage token, remove the demo data, or never press retry on its launch messages.** They failed because staging has no SMS, and retrying would text the made-up numbers, which may belong to real people.

## Grafana Cloud: logs, metrics, traces

Stack: https://maroonstarfish2393.grafana.net (India region). The API sends all three over OTLP, using `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` on Render. Both come from the stack's **Connections → OpenTelemetry (OTLP)** page.

**Logs:** each line Render's console shows is also sent to Grafana, scrubbed the same way.
- **Find them:** Explore → the `-logs` data source → `{service_name="gize-admin-api"}`.
- **What you can filter on:** the line is the event (`unhandled_error`, `token_rejected`, …). Its fields can be filtered after a `|`: `| category="security"`, `| request_id="…"`, `| route="/v1/riders/{rider_id}"`.
- **Logs and traces are linked:** a line logged during a request carries its trace.

**Metrics:** the `-prom` data source. Requests are `http_server_duration_milliseconds_count` by `http_target` (the route pattern) and `http_status_code`. Database and Redis timings are `db_client_operation_duration_seconds`.

**Traces:** the `-traces` data source. Search by service name `gize-admin-api`; requests are named by their route (`GET /v1/riders/{rider_id}`).

**If nothing arrives:**
- Check the two Render settings: a wrong value stops the API from starting, and Render keeps the old version.
- Check the token hasn't been deleted in Grafana.
- On staging, remember it sleeps when idle: no requests, no data.

## Alerts

The rules are files in the repo: `ops/grafana/logs.rules.yaml` and `ops/grafana/metrics.rules.yaml`. Grafana imports them as Grafana-managed rules you can edit. `tests/test_alert_rules.py` keeps them matching the code: an event renamed in the code fails the tests instead of silencing its alert.

**Installed (2026-11-09):**
- The seven rules are in the `gize-api` folder, in two subfolders named after their files: `metrics.rules.yaml` and `logs.rules.yaml`.
- Grafana's folder UID for `gize-api` is `ft62tc`.

**Updating them after a merge that changes a file:** send the file to Grafana's import API. Each file is one rule group, and sending it again replaces that group in place, so nothing needs deleting first. The import form (**Alerting → Alert rules → Import**) handles only the metrics file: it offers no logs (Loki) data source.

1. In Grafana, go to **Administration → Users and access → Service accounts → Add service account**, and create `gize-api-rules` with the **Editor** role. Add a token to it and copy the token.
2. In a terminal at the repo root, run the command below for the file that changed:
   - **Logs:** `FILE=logs DS=grafanacloud-logs`
   - **Metrics:** `FILE=metrics DS=grafanacloud-prom`

   It asks for the token without showing it, and keeps it out of your shell history.

   ```bash
   read -rs GRAFANA_TOKEN && FILE=logs DS=grafanacloud-logs && \
   uv run python -c "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))['groups'][0]))" \
     "ops/grafana/${FILE}.rules.yaml" \
   | curl --fail-with-body -sS -X POST \
       "https://maroonstarfish2393.grafana.net/api/convert/prometheus/config/v1/rules/${FILE}.rules.yaml" \
       -H "Authorization: Bearer ${GRAFANA_TOKEN}" -H "Content-Type: application/json" \
       -H "X-Grafana-Alerting-Datasource-UID: ${DS}" -H "X-Grafana-Alerting-Folder-UID: ft62tc" \
       --data-binary @- ; unset GRAFANA_TOKEN
   ```

   `{"status":"success"…}` means it's done.
3. **Delete the token** in the service account. Keep the account itself for next time.
4. In **Alert rules**, check that the `gize-api` rules show **Normal**, with no error, after a minute.

Every rule except the two production-only ones counts each environment separately and names it in the alert's summary, so a staging problem never reads like a production outage.

**Where alerts go:** **Alerting → Contact points**. The default email contact point gets everything. Set it to the address that should be woken, then press **Test**.

**Telegram (paused):** no code change is needed.
1. Create a Telegram group, then message **@BotFather**, send `/newbot`, and keep the token. It's a secret: it goes into Grafana only, never the repo, Render or chat.
2. Add the bot to the group and send a message there.
3. Get the chat ID: open `https://api.telegram.org/bot<token>/getUpdates` in your browser and use the `chat.id` of your message (a negative number for groups).
4. **Contact points → New → Telegram**: enter the token and chat ID, then **Test**.
5. Under **Notification policies**, send `severity=critical` there too.

**On staging, two gaps are known and alert nowhere.** Startup warnings come again at every wake, so their alert (GizeApiMisconfigured) is production-only:
- `observability_disabled setting=sentry_dsn`: Sentry isn't set up.
- `sms_disabled`: AfroMessage isn't set.

### GizeApiSecurityEventsSpike
More than 20 forged or wrong tokens, refused staff, denied requests or rate-limit hits in 5 minutes, in one environment. Missing and expired tokens are left out: a tab left open after its session ends sends those.
- **Look:** in Explore, run `{service_name="gize-admin-api"} | category="security"` and look at `reason`, `route` and `staff_id`. One staff member with a stale token is noise; many reasons across routes is someone probing.
- **Do:** if it's an attack, the rate limits already slow it, and a deactivated account (`staff_access_denied reason=inactive`) stays locked out. If one account is the source and looks taken over, deactivate it.

### GizeApiStaffAccessChanged
A staff account was invited, re-activated, de-activated or changed role. The first admin, made with the command line at setup, isn't covered: that command ships nothing, so its `first_admin_created` line is in its own output and in Sentry.
- **Look:** the line's `actor_staff_id` (who) and `staff_id` (whom), and the role.
- **Do:** confirm it was meant with the admin who did it. If not, reverse it from the portal, and rotate that admin's password in Supabase.

### GizeApiDependencyUnavailable
The API can't reach Postgres or Redis (`dependency`). Requests that need it answer 503.
- **Look:** Supabase status, and whether the project is paused; Render's Key Value service for Redis.
- **Do:** fix the dependency. Nothing to restart: the API recovers by itself and logs `dependency_recovered`.

### GizeApiUnhandledErrors
A request failed with an error nothing handled: a bug.
- **Look:** the line has `route`, `exception_type`, `exception_frames` and `request_id`. Follow the request ID to its trace and the other lines of that request.
- **Do:** open a fix ticket with that detail. If it blocks riders or staff, roll back to the previous deploy ("Rolling back").

### GizeApiMisconfigured
Production started without a setting it needs: `observability_disabled` names it, and `sms_disabled` means `AFROMESSAGE_TOKEN` is missing. A missing Grafana endpoint can't alert here, since nothing is shipped then; production refuses to start without it unless explicitly opted out.
- **Do:** set it on Render and redeploy ("Production secrets" once production exists).

### GizeApiServerErrors
More than 5% of requests answered 5xx, out of at least 20, for 2 minutes.
- **Look:**
  - 503s usually come with GizeApiDependencyUnavailable;
  - 500s with GizeApiUnhandledErrors;
  - split by `http_target` to see which route.
- **Do:** as for those two. After a deploy, roll back first and investigate after.

### GizeApiSmsFailing
Production: 3 or more SMS sends failed in 15 minutes (AfroMessage unavailable, refusing or timing out).
- **Known gap until GZ-070:** failures in a series' first minute (after a deploy or restart, say) aren't counted. A burst right after a deploy can go unalerted.
- **What riders see:** phone codes answer 503, so people can't register on the website. Launch messages are marked failed, and offers show `sms_status: failed`.
- **Look:**
  - AfroMessage's status, and your balance and sender name in its dashboard;
  - in Explore, `gize_sms_send_duration_seconds_count` split by `outcome` and `purpose`.
  - `rejected` usually means the token, the sender name or the balance; `unavailable` and `timeout` are their side.
- **Do:**
  - fix the account if it's ours;
  - tell callers to try again later;
  - once sends work, retry the failed launch messages from the route (`launch-messages/retry`);
  - call the people whose offers show a failed SMS. An offer's SMS isn't resent.

### GizeApiSilent
Production sent no telemetry for 15 minutes, having sent some in the last day.
- **Look:** `/health/ready` from your machine.
  - If it answers, the export is broken: check the Grafana token and the two Render settings.
  - If it doesn't, the API is down: check Render's events.
