# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# One image, three commands: `api` (default), `worker` and `migrate`. See docker/entrypoint.sh.

# Keep the patch release in step with UV_PYTHON in .github/workflows/ci.yml.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.30@sha256:93b61e21202b1dab861092748e46bbd6e0e41dd84f59b9174efd2353186e1b47

FROM ${UV_IMAGE} AS uv

# --- build: resolve the locked runtime dependencies into /opt/venv -----------------------
FROM ${PYTHON_IMAGE} AS builder
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
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 gize \
    && useradd --system --uid 10001 --gid gize --no-create-home --shell /usr/sbin/nologin gize
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
