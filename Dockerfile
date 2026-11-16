# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# One image, three commands: `api` (default), `worker` and `migrate`. See docker/entrypoint.sh.

# Images are written on their FROM lines, not in ARGs: Dependabot reads only literal ones
# (GZ-065). Both Python stages use the same image, which it updates together; keep its patch
# release in step with UV_PYTHON, and uv's with UV_VERSION, in the workflows (RUNBOOK.md,
# "Dependency updates"; tests/test_ci_config.py fails until they match).

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

# --- build: resolve the locked runtime dependencies into /opt/venv -----------------------
FROM python:3.12.16-slim-trixie@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime: no build tools, no uv, non-root ---------------------------------------------
FROM python:3.12.16-slim-trixie@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 gize \
    && useradd --system --uid 10001 --gid gize --no-create-home --shell /usr/sbin/nologin gize
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
# Supabase's public root CA, for DATABASE_SSL_ROOT_CERT (ssl=verify-full); not a secret.
# Owned by root, so the app can read it but not change it. No --chmod: some builders
# (Render's) apply it to the directory COPY creates as well, which the app then can't enter.
COPY docker/certs/supabase-root-2021-ca.crt /app/certs/supabase-root-2021-ca.crt
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER 10001:10001
EXPOSE 8000
# Liveness of the API process (/health/live); a no-op in worker and migrate containers.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-m", "gize_api.healthcheck"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
