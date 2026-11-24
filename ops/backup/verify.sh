#!/usr/bin/env bash
# GZ-086: prove a dump restores. Into a fresh database on a throwaway Postgres 17 server:
# every table back with its rows (one transaction, stopping at the first error) and the
# migration version present. Prints counts only; drops the database after.
#
#   SCRATCH_URL=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres ops/backup/verify.sh <dump>
#
# SCRATCH_URL is a superuser on a server that holds nothing else of value: the check
# creates the database gize_restore_check there, and the roles gize_app, gize_migrator and
# gize_backup (without sign-in) if they're missing. Postgres's own error messages can quote
# rows: the nightly job keeps them out of its public log.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ "$#" -ne 1 ]; then
  echo "usage: SCRATCH_URL=... $0 <dump file>" >&2
  exit 2
fi
pg_connection SCRATCH_URL

dump_dir="$(cd "$(dirname "$1")" && pwd)"
dump_name="$(basename "$1")"

pg_docker_run "${dump_dir}:/in:ro" bash -s -- "${dump_name}" <<'INSIDE'
set -euo pipefail
dump="/in/$1"
target="${PGURL%/*}/gize_restore_check"
psql -X --dbname="$PGURL" -v ON_ERROR_STOP=1 -q <<'SQL'
SET client_min_messages = warning;
DROP DATABASE IF EXISTS gize_restore_check WITH (FORCE);
CREATE DATABASE gize_restore_check;
DO $$
DECLARE
    name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['gize_app', 'gize_migrator', 'gize_backup'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', name);
        END IF;
    END LOOP;
END
$$;
SQL
pg_restore --dbname="$target" --exit-on-error --single-transaction --no-password "$dump"
in_dump="$(pg_restore --list "$dump" | grep -c ' TABLE DATA gize ' || true)"
restored="$(psql -X --dbname="$target" -Atc "SELECT count(*) FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'gize' AND c.relkind = 'r'")"
versions="$(psql -X --dbname="$target" -Atc 'SELECT count(*) FROM gize.alembic_version')"
version="$(psql -X --dbname="$target" -Atc 'SELECT min(version_num) FROM gize.alembic_version')"
psql -X --dbname="$PGURL" -q -c "DROP DATABASE gize_restore_check WITH (FORCE)"
if [ "$in_dump" -lt 1 ] || [ "$in_dump" != "$restored" ] || [ "$versions" != 1 ]; then
  echo "The copy is incomplete: ${in_dump} tables in the dump, ${restored} restored," \
    "${versions} migration versions." >&2
  exit 1
fi
echo "The copy restores: ${restored} tables with their rows, at migration ${version}."
INSIDE
