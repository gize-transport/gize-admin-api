"""ASGI entry point.

Placeholder app so the Docker `api` command has something to serve; GZ-003 replaces it
with the settings-driven app factory. Docs and schema stay off until GZ-003 decides
per environment.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Gize Admin API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
