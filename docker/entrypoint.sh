#!/bin/sh
# Container entry point: `api` serves HTTP, `worker` runs the Arq background worker.
# Arguments are matched against a fixed list; nothing is passed through to a shell.
# Proxy headers are ignored: the client IP policy lives in core/ratelimit.py (GZ-009).
# The access log is off because it would print raw query strings; GZ-005 adds a scrubbed one.
set -eu

case "${1:-api}" in
  api)
    exec uvicorn gize_api.main:app \
      --host 0.0.0.0 \
      --port 8000 \
      --no-proxy-headers \
      --no-server-header \
      --no-access-log \
      --timeout-graceful-shutdown 30
    ;;
  worker)
    exec arq gize_api.worker.WorkerSettings
    ;;
  *)
    echo "usage: entrypoint.sh [api|worker]" >&2
    exit 64
    ;;
esac
