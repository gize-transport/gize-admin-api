#!/bin/sh
# Container entry point: `api` serves HTTP, `worker` runs the Arq background worker.
# Arguments are matched against a fixed list; nothing is passed through to a shell.
# Proxy headers are ignored: the client IP policy lives in core/ratelimit.py (GZ-009).
# The access log is off because it would print raw query strings; GZ-005 adds a scrubbed one.
set -eu

if [ "$#" -gt 1 ]; then
  echo "usage: entrypoint.sh [api|worker|migrate]" >&2
  exit 64
fi

case "${1:-api}" in
  api)
    exec uvicorn gize_api.main:create_app \
      --factory \
      --lifespan on \
      --ws none \
      --host 0.0.0.0 \
      --port 8000 \
      --no-proxy-headers \
      --no-server-header \
      --no-access-log \
      --timeout-graceful-shutdown 30
    ;;
  worker)
    # Our own runner, not Arq's CLI (which would replace the JSON logging).
    exec python -m gize_api.worker
    ;;
  migrate)
    # The release step: runs as gize_migrator (MIGRATOR_DATABASE_URL), before the new API starts.
    exec alembic upgrade head
    ;;
  *)
    echo "usage: entrypoint.sh [api|worker|migrate]" >&2
    exit 64
    ;;
esac
