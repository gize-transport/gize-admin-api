# Runbook – Gize Admin API

How to set up, deploy, roll back and look after the hosted API. Staging only for now. Production gets its own section when it is set up: a paid plan, its own Supabase project, observability required.

## Staging at a glance

| Piece | Where | Notes |
| --- | --- | --- |
| API | Render web service `gize-api-staging` (free plan, Frankfurt) | Built from the `Dockerfile`, configured by `render.yaml`. It sleeps after 15 minutes without traffic, so the first request after that takes about a minute. |
| Redis | Render Key Value `gize-redis-staging` (free plan) | Private network only. It loses its data on restart, which only resets rate limits for now. |
| Database and sign-in | Supabase project `pahktmbnbyulqkhdruci` (eu-west-1) | Postgres schema `gize` plus Supabase Auth. The free plan has no automatic backups and pauses after about a week without activity (unpause it in the dashboard). |
| Deploys | `.github/workflows/deploy-staging.yml` | After CI passes on `main`: migrations, then Render's deploy hook for that commit. |

Not running on staging yet:
- **The worker:** it only clears old idempotency keys until M3 sends SMS, and Render's free plan has no workers.
- **CORS origins:** the frontend isn't deployed yet.
- **`CLIENT_IP_HEADER`:** Render documents no trustworthy client-IP header; this must be settled before the public sign-up routes (M3).
- **Sentry and Grafana:** optional on staging. The API logs `observability_disabled` at startup.

## Staging secrets

Never put any of these in chat, code, tickets or logs. Each lives in exactly the places listed.

| Secret | Lives in | Used by |
| --- | --- | --- |
| `DATABASE_URL` (`gize_app`) | Render (`gize-api-staging` → Environment); your local `.env.staging` | the API |
| `MIGRATOR_DATABASE_URL` (`gize_migrator`) | GitHub → Settings → Secrets and variables → Actions: `STAGING_MIGRATOR_DATABASE_URL`; `.env.staging` | the deploy workflow's migrations |
| `SUPABASE_SECRET_KEY` | Render; `.env.staging` | staff invites; the first-admin command |
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
6. **Render:**
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

Deploys run one at a time, in merge order, and never cancel each other.

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
| Deploy hook URL | 1. Regenerate it in Render (`gize-api-staging` → Settings). 2. Update `RENDER_STAGING_DEPLOY_HOOK_URL` in GitHub. The old URL stops working at once. |
| The `postgres` database password | Reset it in Supabase (Project Settings → Database). Nothing deployed uses it. |

After any leak, rotate first and investigate second. A leaked `SUPABASE_SECRET_KEY` can create users: check Authentication → Users.

## Backups and restoring

Supabase's free plan takes **no automatic backups**, so staging data can't be restored. Before a risky migration on staging, take a copy from your machine with Postgres 17 client tools:

```bash
pg_dump --schema=gize --format=custom --file=staging-$(date +%F).dump "<the postgres session-pooler URL>"
```

To restore it into the `gize` schema, run `pg_restore --clean --if-exists --schema=gize --dbname="<URL>" <file>`, then re-run the bootstrap to reset the grants. For production, choose a paid plan with daily backups or point-in-time recovery before launch.

## What the signals mean

There's no alerting yet: shipping logs and alert rules come with GZ-066.

| You see | It means | Do |
| --- | --- | --- |
| `/health/ready` not 200, or Render marks the deploy failed | The database or Redis is unreachable from the new version | Check the Supabase status and that the project isn't paused. Check `DATABASE_URL` in Render. Read the Render logs for `database_unavailable` and its SQLSTATE. |
| Requests take about a minute after a quiet spell | The free instance was asleep | Nothing. A paid plan doesn't sleep. |
| `503` with `Retry-After` | The database was busy or briefly down | Retry. If it persists, check Supabase. |
| `401` on every request | The token is from another Supabase project, or the project uses the legacy JWT secret | Check `SUPABASE_URL` and the JWT signing keys. |
| `403` for a signed-in user | They aren't active staff in this database | An admin invites them, or the first-admin step is missing. |
| `observability_disabled` at startup | Sentry or Grafana isn't configured | Expected on staging. |
