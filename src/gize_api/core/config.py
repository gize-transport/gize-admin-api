"""Application settings, read once from environment variables at startup.

Every variable is listed in `.env.example`. Required values have no defaults, so the app
refuses to start without them. Errors name the setting and the problem, never its value:
a malformed URL can carry a password.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from pydantic import (
    Field,
    FilePath,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
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
        # `NAME=` (as in .env.example) means unset, not an empty value to validate.
        env_ignore_empty=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: Environment
    redis_url: SecretStr
    # As the least-privileged gize_app role; migrations use MIGRATOR_DATABASE_URL instead.
    database_url: SecretStr
    # The Supabase project URL (https://<ref>.supabase.co): sign-in tokens are verified
    # against its public signing keys and must name its Auth service as issuer.
    supabase_url: str
    # CA certificate (PEM) that the database's certificate must chain to (ssl=verify-full);
    # Supabase's is downloaded from the project's database settings. Unset: system CAs.
    database_ssl_root_cert: FilePath | None = None
    # Connections per process: pool size plus overflow. Every process (API replicas, the
    # worker, the old release during a deploy) counts against the pooler's client limit.
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    # The request header holding the client's IP, written by a trusted proxy in front of
    # the API (`CF-Connecting-IP` behind Cloudflare). Unset: the socket address. Never set
    # it without such a proxy: the header would let any client choose its rate-limit key.
    client_ip_header: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(default=(), max_length=20)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Observability: each is off when unset. Production refuses to start without both unless
    # OBSERVABILITY_REQUIRED=false; staging (and an opted-out production) warns at startup.
    observability_required: bool = True
    sentry_dsn: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: SecretStr | None = None
    otel_service_name: str = Field(default="gize-admin-api", pattern=r"^[a-z0-9-]{1,63}$")
    # Share of new traces recorded (0..1). Inbound client trace context is ignored.
    otel_traces_sampler_ratio: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)
    release: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("client_ip_header")
    @classmethod
    def _lower_case_header(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None  # how ASGI spells header names

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

    @field_validator("database_url")
    @classmethod
    def _database_url_shape(cls, value: SecretStr) -> SecretStr:
        if not is_database_url(value.get_secret_value()):
            raise ValueError(
                "must be postgresql://user:password@host[:port]/database[?ssl=require|verify-full]"
            )
        return value

    @field_validator("supabase_url")
    @classmethod
    def _supabase_url_shape(cls, value: str) -> str:
        parts = _safe_split(value)
        if (
            parts is None
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("must be the project URL, e.g. https://<project-ref>.supabase.co")
        return value.rstrip("/")

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
    def _observability_in_production(self) -> Self:
        if (
            self.app_env is Environment.PRODUCTION
            and self.observability_required
            and self.missing_observability()
        ):
            missing = ", ".join(self.missing_observability())
            raise ValueError(
                f"production requires {missing} (set OBSERVABILITY_REQUIRED=false to opt out)"
            )
        return self

    @model_validator(mode="after")
    def _database_tls(self) -> Self:
        problem = database_tls_problem(
            self.app_env, self.database_ssl, root_cert=self.database_ssl_root_cert is not None
        )
        if problem is not None:
            raise ValueError(f"database_url {problem}")
        return self

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

    @property
    def database_ssl(self) -> str | None:
        """The `ssl` option from DATABASE_URL (`require`, `verify-full`), or None."""
        return database_ssl_mode(self.database_url.get_secret_value())

    @property
    def supabase_host(self) -> str:
        """The project's host (with a port if the URL names one), for the outbound allowlist."""
        return urlsplit(self.supabase_url).netloc

    @property
    def supabase_auth_issuer(self) -> str:
        """The `iss` claim Supabase Auth puts in this project's access tokens."""
        return f"{self.supabase_url}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        """Where the project publishes its public signing keys."""
        return f"{self.supabase_auth_issuer}/.well-known/jwks.json"

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


_DATABASE_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+asyncpg"})
_DATABASE_SSL_MODES = frozenset({"require", "verify-full"})
_DATABASE_NAME = re.compile(r"/[A-Za-z0-9_-]{1,63}")


def is_database_url(url: str) -> bool:
    """postgresql://user[:password]@host[:port]/database, with at most an `ssl` option."""
    parts = _safe_split(url)
    if parts is None or parts.scheme not in _DATABASE_SCHEMES or parts.fragment:
        return False
    if not parts.hostname or not parts.username or not _DATABASE_NAME.fullmatch(parts.path):
        return False
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) - {"ssl"}:
        return False
    return all(mode in _DATABASE_SSL_MODES for mode in query.get("ssl", []))


def database_ssl_mode(url: str) -> str | None:
    """The `ssl` option of a database URL (`require`, `verify-full`), or None."""
    return parse_qs(urlsplit(url).query).get("ssl", [None])[0]


def database_tls_problem(
    app_env: Environment, ssl_mode: str | None, *, root_cert: bool
) -> str | None:
    """What is wrong with a database connection's TLS for `app_env`, or None.

    `require` encrypts but accepts any certificate, so staging and production verify it
    (`verify-full`: the certificate chains to a trusted CA and matches the host name).
    Staging verifies too, so a certificate problem shows up there, not at launch.
    """
    if root_cert and ssl_mode != "verify-full":
        return "needs ssl=verify-full when DATABASE_SSL_ROOT_CERT is set"
    if app_env in {Environment.STAGING, Environment.PRODUCTION} and ssl_mode != "verify-full":
        return "must set ssl=verify-full in staging and production"
    return None


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


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}")
_HEADER_VALUE = re.compile(r"[^\x00-\x1f\x7f]{1,4096}")


def _parse_headers(text: str) -> dict[str, str] | None:
    """Parse `name=value,…`, percent-decoded as the OTel spec (and Grafana's snippet) writes it."""
    headers: dict[str, str] = {}
    for pair in text.split(","):
        name, sep, value = pair.partition("=")
        name, value = unquote(name.strip()), unquote(value.strip())
        if not sep or not _HEADER_NAME.fullmatch(name) or not _HEADER_VALUE.fullmatch(value):
            return None
        headers[name] = value
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
