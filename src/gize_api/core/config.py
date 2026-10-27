"""Application settings, read once from environment variables at startup.

Every variable is listed in `.env.example`. Required values have no defaults, so the app
refuses to start without them. Errors name the setting and the problem, never its value:
a malformed URL can carry a password.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ConfigError(Exception):
    """The environment does not hold a valid configuration. The message is safe to log."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: Environment
    redis_url: SecretStr
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(default=(), max_length=20)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Observability: each is off when unset. staging/production log a warning at startup.
    sentry_dsn: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: SecretStr | None = None
    otel_service_name: str = Field(default="gize-admin-api", pattern=r"^[a-z0-9-]{1,63}$")
    release: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _origins_are_bare(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            if not _is_bare_origin(origin):
                raise ValueError(
                    "each origin must be lowercase scheme://host[:port], "
                    "e.g. https://admin.example.com"
                )
        return origins

    @field_validator("redis_url")
    @classmethod
    def _redis_url_shape(cls, value: SecretStr) -> SecretStr:
        # Options in a query string would override the client's timeouts, so none allowed.
        parts = _safe_split(value.get_secret_value())
        if (
            parts is None
            or parts.scheme not in {"redis", "rediss"}
            or not parts.hostname
            or parts.query
            or parts.fragment
            or not _REDIS_DB_PATH.fullmatch(parts.path)
        ):
            raise ValueError("must be redis://[user:password@]host[:port][/db] (or rediss://)")
        return value

    @field_validator("sentry_dsn")
    @classmethod
    def _sentry_dsn_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        parts = _safe_split(value.get_secret_value())
        if parts is None or parts.scheme != "https" or not parts.hostname:
            raise ValueError("must be the https DSN from the Sentry project settings")
        return value

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _otlp_endpoint_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = _safe_split(value)
        if (
            parts is None
            or parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("must be the OTLP/HTTP base URL, e.g. https://otlp.example.com/otlp")
        return value.rstrip("/")

    @field_validator("otel_exporter_otlp_headers")
    @classmethod
    def _otlp_headers_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and _parse_headers(value.get_secret_value()) is None:
            raise ValueError("must be comma-separated name=value pairs")
        return value

    @model_validator(mode="after")
    def _https_otlp_outside_local(self) -> Self:
        endpoint = self.otel_exporter_otlp_endpoint
        if (
            endpoint is not None
            and self.app_env in {Environment.STAGING, Environment.PRODUCTION}
            and not endpoint.startswith("https://")
        ):
            raise ValueError("otel_exporter_otlp_endpoint must use https in staging and production")
        return self

    @model_validator(mode="after")
    def _https_origins_outside_local(self) -> Self:
        if self.app_env in {Environment.STAGING, Environment.PRODUCTION} and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("cors_allowed_origins must use https in staging and production")
        return self

    def otlp_headers(self) -> dict[str, str] | None:
        """OTLP exporter headers (e.g. Grafana Cloud auth), or None."""
        if self.otel_exporter_otlp_headers is None:
            return None
        return _parse_headers(self.otel_exporter_otlp_headers.get_secret_value())

    def missing_observability(self) -> list[str]:
        """Observability settings that are unset; startup warns about them outside local/test."""
        missing = []
        if self.sentry_dsn is None:
            missing.append("sentry_dsn")
        if self.otel_exporter_otlp_endpoint is None:
            missing.append("otel_exporter_otlp_endpoint")
        return missing

    @property
    def docs_enabled(self) -> bool:
        """Interactive docs and the schema endpoint are served only where staff test."""
        return self.app_env in {Environment.LOCAL, Environment.STAGING}


_REDIS_DB_PATH = re.compile(r"(/\d{0,2})?")


def _safe_split(url: str) -> SplitResult | None:
    """Parse a URL, or None if it is malformed.

    urllib's own error messages quote the URL, password included, so they must never reach
    a validation error.
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return None
    return parts


def _is_bare_origin(origin: str) -> bool:
    parts = _safe_split(origin)
    return (
        parts is not None
        and parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and parts.username is None
        and origin == f"{parts.scheme}://{parts.netloc}"
        and origin == origin.lower()
    )


def _parse_headers(text: str) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    for pair in text.split(","):
        name, sep, value = pair.partition("=")
        if not sep or not name.strip() or not value.strip():
            return None
        headers[name.strip()] = value.strip()
    return headers


def load_settings() -> Settings:
    """Read settings from the environment, or raise ConfigError naming what is wrong."""
    try:
        return Settings()  # values come from the environment
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid configuration: {problems}") from None
