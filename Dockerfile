# syntax=docker/dockerfile:1
# One image, two commands: `api` (default) and `worker`. See docker/entrypoint.sh.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753
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
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["api"]
