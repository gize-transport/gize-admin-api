#!/usr/bin/env bash
# GZ-086: dump schema gize into one custom-format file, as gize_backup (read-only), with
# Postgres 17's pg_dump. Run nightly (.github/workflows/backup-production.yml) and in CI's
# migrations job.
#
#   DUMP_URL=<URL> [DATABASE_SSL_ROOT_CERT=<CA file>] ops/backup/dump.sh <file>
#
# DUMP_URL is gize_backup's (BACKUP_DATABASE_URL), or gize_migrator's for a copy by hand:
# the owner reads past RLS. --enable-row-security reads through the policies, so a table
# under RLS without gize_backup's read policy, or with a restrictive one, would dump short,
# not fail: the first step checks every table and dumps nothing if one can't be read whole.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ "$#" -ne 1 ]; then
  echo "usage: DUMP_URL=... $0 <output file>" >&2
  exit 2
fi
pg_connection DUMP_URL

out_dir="$(cd "$(dirname "$1")" && pwd)"
out_name="$(basename "$1")"

pg_docker_run "${out_dir}:/out" bash -s -- "${out_name}" <<'INSIDE'
set -euo pipefail
unreadable="$(psql -X --dbname="$PGURL" -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT coalesce(string_agg(c.relname, ', ' ORDER BY c.relname), '')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'gize' AND c.relkind IN ('r', 'p')
AND (
    NOT has_table_privilege(c.oid, 'SELECT')
    OR (
        c.relrowsecurity
        AND NOT (c.relowner = current_user::regrole AND NOT c.relforcerowsecurity)
        AND (
            NOT EXISTS (
                SELECT 1 FROM pg_policy p
                WHERE p.polrelid = c.oid AND p.polpermissive AND p.polcmd IN ('r', '*')
                AND current_user::regrole = ANY (p.polroles)
                AND pg_get_expr(p.polqual, p.polrelid) = 'true'
            )
            OR EXISTS (
                SELECT 1 FROM pg_policy r
                WHERE r.polrelid = c.oid AND NOT r.polpermissive AND r.polcmd IN ('r', '*')
                AND (0::oid = ANY (r.polroles) OR current_user::regrole = ANY (r.polroles))
            )
        )
    )
)
SQL
)"
if [ -n "${unreadable}" ]; then
  echo "The backup role can't read every row of: ${unreadable}. Nothing was dumped." >&2
  exit 1
fi
exec pg_dump --dbname="$PGURL" --schema=gize --format=custom --enable-row-security \
  --lock-wait-timeout=5s --no-password --file="/out/$1"
INSIDE
