#!/usr/bin/env bash
# GZ-086: put a backup back. Replaces every object in schema gize, in the database
# RESTORE_URL points at, with the dump's (dropped and recreated in one transaction: on any
# error nothing changes), as gize_migrator, the schema's owner; the schema itself stays.
#
#   RESTORE_URL=<gize_migrator's URL> [DATABASE_SSL_ROOT_CERT=<CA file>] \
#     ops/backup/restore.sh <decrypted dump>
#
# Stop deploys and suspend the API first (RUNBOOK.md, "Restoring"). It shows where it will
# write and both migration versions, and refuses unless they match (or the database has no
# tables yet): restoring an older copy over a newer schema would leave the newer objects
# behind. It asks you to type "restore" (RESTORE_CONFIRM=restore answers for CI).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ "$#" -ne 1 ]; then
  echo "usage: RESTORE_URL=... $0 <dump file>" >&2
  exit 2
fi
pg_connection RESTORE_URL

dump_dir="$(cd "$(dirname "$1")" && pwd)"
dump_name="$(basename "$1")"
where="${PGURL#*@}"
where="${where%%\?*}"

versions="$(pg_docker_run "${dump_dir}:/in:ro" bash -s -- "${dump_name}" <<'INSIDE'
set -euo pipefail
copy="$(pg_restore --data-only --table=alembic_version --file=- "/in/$1" \
  | awk '/^COPY /{ getline; print; exit }')"
current=""
if [ "$(psql -X --dbname="$PGURL" -At -c "SELECT to_regclass('gize.alembic_version') IS NOT NULL")" = t ]; then
  current="$(psql -X --dbname="$PGURL" -At -c 'SELECT min(version_num) FROM gize.alembic_version')"
fi
echo "${copy} ${current}"
INSIDE
)"
copy="${versions%% *}"
current="${versions#* }"
if [ -z "${copy}" ]; then
  echo "The copy has no migration version: it isn't a Gize backup. Nothing changed." >&2
  exit 1
fi
echo "This replaces everything in schema gize on ${where}."
echo "The copy is at migration ${copy}; the database is at ${current:-none (no tables yet)}."
if [ -n "${current}" ] && [ "${current}" != "${copy}" ]; then
  echo "They differ, so nothing changed: the database must be at the copy's migration first." \
    "Ask the developer." >&2
  exit 1
fi
if [ "${RESTORE_CONFIRM:-}" != "restore" ]; then
  if ! { : < /dev/tty; } 2> /dev/null; then
    echo "No terminal to confirm on, so nothing changed." >&2
    exit 1
  fi
  read -r -p 'Type restore to go ahead: ' answer < /dev/tty
  if [ "${answer}" != "restore" ]; then
    echo "Nothing changed." >&2
    exit 1
  fi
fi

pg_docker_run "${dump_dir}:/in:ro" bash -s -- "${dump_name}" <<'INSIDE'
set -euo pipefail
# Everything but the schema itself: gize_migrator owns gize but may not create schemas
# (the bootstrap made it), so the schema stays and every object in it is replaced.
pg_restore --list "/in/$1" | grep -vE '^[0-9]+; [0-9]+ [0-9]+ SCHEMA - gize ' > /tmp/contents
pg_restore --dbname="$PGURL" --use-list=/tmp/contents --clean --if-exists \
  --single-transaction --exit-on-error --no-password "/in/$1"
version="$(psql -X --dbname="$PGURL" -At -c 'SELECT version_num FROM gize.alembic_version')"
echo "Restored. The database is at migration ${version}."
INSIDE
