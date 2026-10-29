"""GZ-003: settings load from the environment and fail fast without leaking values."""

import pytest
from pydantic import SecretStr

from gize_api.core.config import ConfigError, Environment, Settings, load_settings

REQUIRED = {
    "APP_ENV": "test",
    "REDIS_URL": "redis://:s3cret-redis-pass@127.0.0.1:6379/0",
    "DATABASE_URL": "postgresql://gize_app:s3cret-db-pass@127.0.0.1:5432/gize?ssl=verify-full",
    "SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co",
}


def _set(env: pytest.MonkeyPatch, **values: str) -> None:
    # Production refuses to start without Sentry/OTLP; tests about other settings opt out.
    for name, value in {**REQUIRED, "OBSERVABILITY_REQUIRED": "false", **values}.items():
        env.setenv(name, value)


def test_loads_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, CORS_ALLOWED_ORIGINS="http://localhost:3000, https://admin.example.com")
    settings = load_settings()
    assert settings.app_env is Environment.TEST
    assert settings.redis_url.get_secret_value() == REQUIRED["REDIS_URL"]
    assert settings.cors_allowed_origins == ("http://localhost:3000", "https://admin.example.com")
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_setting_refuses_to_start(
    clean_env: pytest.MonkeyPatch, missing: str
) -> None:
    _set(clean_env)
    clean_env.delenv(missing)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert missing.lower() in str(exc_info.value)


def test_config_error_never_echoes_values(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, REDIS_URL="http://:leaked-password@redis.internal:6379")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    message = str(exc_info.value)
    assert "redis_url" in message
    assert "leaked-password" not in message
    assert "redis.internal" not in message
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


def test_secrets_hidden_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env)
    settings = load_settings()
    assert "s3cret-redis-pass" not in repr(settings)
    assert "s3cret-redis-pass" not in str(settings.model_dump())


def test_no_secret_field_has_a_default() -> None:
    for name, field in Settings.model_fields.items():
        if field.annotation is SecretStr:
            assert field.is_required(), f"{name} must not have a default"


def test_settings_are_immutable(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env)
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("app_env", "value"),
    [
        ("test", "redis://h:6379/0"),
        ("test", "rediss://h:6380/0"),
        ("production", "rediss://h:6380/0"),
    ],
)
def test_redis_url_schemes_accepted(
    clean_env: pytest.MonkeyPatch, app_env: str, value: str
) -> None:
    _set(clean_env, APP_ENV=app_env, REDIS_URL=value)
    assert load_settings().redis_url.get_secret_value() == value


@pytest.mark.parametrize(
    "value",
    [
        "http://h:6379",
        "h:6379",
        "unix:///tmp/redis.sock",
        "",
        "redis://h:99999/0",  # port out of range
        "redis://h:6379/0?socket_timeout=99",  # would override the 1 s timeouts
        "redis://h:6379/0#frag",
        "redis://h:6379/not-a-db",
        "redis://h:6379/0/extra",
    ],
)
def test_redis_url_rejected(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, REDIS_URL=value)
    with pytest.raises(ConfigError, match="redis_url"):
        load_settings()


@pytest.mark.parametrize("value", ["redis://h:6379", "redis://h:6379/", "redis://h/15"])
def test_redis_url_db_path_optional(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, REDIS_URL=value)
    assert load_settings().redis_url.get_secret_value() == value


@pytest.mark.parametrize(
    ("name", "value", "secret"),
    [
        # urlsplit's own error message quotes the netloc, password included.
        ("REDIS_URL", "redis://:pa\uff03ssLEAK@h:6379/0", "ssLEAK"),
        ("REDIS_URL", "redis://:hunter2LEAK@h:notaport/0", "hunter2LEAK"),
        ("CORS_ALLOWED_ORIGINS", "https://user:pa\uff03ssLEAK@admin.example.com", "ssLEAK"),
    ],
)
def test_parser_errors_never_echo_secrets(
    clean_env: pytest.MonkeyPatch, name: str, value: str, secret: str
) -> None:
    _set(clean_env, **{name: value})
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert secret not in str(exc_info.value)
    assert name.lower() in str(exc_info.value)


@pytest.mark.parametrize("value", ["info", "Info", "DEBUG"])
def test_log_level_any_case(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, LOG_LEVEL=value)
    assert load_settings().log_level == value.upper()


def test_log_level_unknown_rejected(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, LOG_LEVEL="verbose")
    with pytest.raises(ConfigError, match="log_level"):
        load_settings()


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "https://admin.example.com/",
        "https://admin.example.com/path",
        "https://admin.example.com?x=1",
        "ftp://admin.example.com",
        "admin.example.com",
        "https://user:pw@admin.example.com",
        "https://admin.example.com:99999",
        "HTTPS://Admin.Example.com",
    ],
)
def test_malformed_cors_origins_rejected(clean_env: pytest.MonkeyPatch, origins: str) -> None:
    _set(clean_env, CORS_ALLOWED_ORIGINS=origins)
    with pytest.raises(ConfigError, match="cors_allowed_origins"):
        load_settings()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_plain_http_origin_rejected_outside_local_and_test(
    clean_env: pytest.MonkeyPatch, app_env: str
) -> None:
    _set(clean_env, APP_ENV=app_env, CORS_ALLOWED_ORIGINS="http://admin.example.com")
    with pytest.raises(ConfigError, match="cors_allowed_origins"):
        load_settings()


def test_unknown_app_env_rejected(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, APP_ENV="prod")
    with pytest.raises(ConfigError, match="app_env"):
        load_settings()


def test_unknown_init_argument_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Settings(  # type: ignore[call-arg]  # the unknown argument is the point
            app_env=Environment.TEST, redis_url=SecretStr("redis://h:6379/0"), debug=True
        )


@pytest.mark.parametrize(
    ("app_env", "docs"),
    [("local", True), ("test", False), ("staging", True), ("production", False)],
)
def test_docs_enabled_only_in_local_and_staging(
    clean_env: pytest.MonkeyPatch, app_env: str, docs: bool
) -> None:
    _set(clean_env, APP_ENV=app_env)
    assert load_settings().docs_enabled is docs


# --- observability (GZ-005) -------------------------------------------------------------


def test_observability_is_optional(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env)
    settings = load_settings()
    assert settings.sentry_dsn is None
    assert settings.otel_exporter_otlp_endpoint is None
    assert settings.otlp_headers() is None
    assert settings.missing_observability() == ["sentry_dsn", "otel_exporter_otlp_endpoint"]


def test_observability_settings_load(clean_env: pytest.MonkeyPatch) -> None:
    _set(
        clean_env,
        APP_ENV="production",
        DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=verify-full",
        SENTRY_DSN="https://dsnkey987@o1.ingest.sentry.io/42",
        OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.com/otlp/",
        OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic c2VjcmV0, X-Scope = gize",
        OTEL_SERVICE_NAME="gize-admin-api-staging",
        RELEASE="2026.10.27-abc123",
    )
    settings = load_settings()
    assert settings.otel_exporter_otlp_endpoint == "https://otlp.example.com/otlp"
    assert settings.otlp_headers() == {"Authorization": "Basic c2VjcmV0", "X-Scope": "gize"}
    assert settings.missing_observability() == []
    assert "c2VjcmV0" not in repr(settings)
    assert "dsnkey987" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SENTRY_DSN", "http://abc@o1.ingest.sentry.io/42"),
        ("SENTRY_DSN", "not a url"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "ftp://otlp.example.com"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "https://user:pw@otlp.example.com"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example.com?x=1"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "Authorization"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "=value"),
        ("OTEL_SERVICE_NAME", "Gize API"),
        ("RELEASE", "v1 with spaces"),
    ],
)
def test_observability_settings_rejected(
    clean_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set(clean_env, **{name: value})
    with pytest.raises(ConfigError, match=name.lower()):
        load_settings()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_plain_http_otlp_rejected_outside_local_and_test(
    clean_env: pytest.MonkeyPatch, app_env: str
) -> None:
    _set(clean_env, APP_ENV=app_env, OTEL_EXPORTER_OTLP_ENDPOINT="http://otlp.example.com")
    with pytest.raises(ConfigError, match="otel_exporter_otlp_endpoint"):
        load_settings()


def test_production_refuses_to_start_without_observability(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, APP_ENV="production")
    clean_env.delenv("OBSERVABILITY_REQUIRED")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    message = str(exc_info.value)
    assert "sentry_dsn" in message
    assert "otel_exporter_otlp_endpoint" in message
    assert "OBSERVABILITY_REQUIRED" in message


def test_production_starts_with_observability_configured(clean_env: pytest.MonkeyPatch) -> None:
    _set(
        clean_env,
        APP_ENV="production",
        DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=verify-full",
        SENTRY_DSN="https://dsnkey987@o1.ingest.sentry.io/42",
        OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.com/otlp",
    )
    clean_env.delenv("OBSERVABILITY_REQUIRED")
    assert load_settings().observability_required is True


@pytest.mark.parametrize("app_env", ["local", "test", "staging"])
def test_observability_only_required_in_production(
    clean_env: pytest.MonkeyPatch, app_env: str
) -> None:
    _set(clean_env, APP_ENV=app_env)
    clean_env.delenv("OBSERVABILITY_REQUIRED")
    assert load_settings().missing_observability() == ["sentry_dsn", "otel_exporter_otlp_endpoint"]


def test_otlp_headers_are_percent_decoded(clean_env: pytest.MonkeyPatch) -> None:
    # Grafana Cloud's snippet: OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20<b64>"
    _set(clean_env, OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20c2VjcmV0,X%2DScope=a%3Db")
    assert load_settings().otlp_headers() == {"Authorization": "Basic c2VjcmV0", "X-Scope": "a=b"}


@pytest.mark.parametrize(("value", "expected"), [(None, 1.0), ("0", 0.0), ("0.25", 0.25)])
def test_trace_sampler_ratio(
    clean_env: pytest.MonkeyPatch, value: str | None, expected: float
) -> None:
    _set(clean_env, **({} if value is None else {"OTEL_TRACES_SAMPLER_RATIO": value}))
    assert load_settings().otel_traces_sampler_ratio == expected


@pytest.mark.parametrize("value", ["-0.1", "1.5", "abc", "nan"])
def test_trace_sampler_ratio_rejected(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, OTEL_TRACES_SAMPLER_RATIO=value)
    with pytest.raises(ConfigError, match="otel_traces_sampler_ratio"):
        load_settings()


@pytest.mark.parametrize(
    "value",
    ["a=b%0D%0AX-Evil: 1", "bad name=v", "a=v%00", "a b=c"],
)
def test_otlp_headers_with_control_characters_rejected(
    clean_env: pytest.MonkeyPatch, value: str
) -> None:
    _set(clean_env, OTEL_EXPORTER_OTLP_HEADERS=value)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert "otel_exporter_otlp_headers" in str(exc_info.value)
    assert "Evil" not in str(exc_info.value)


# --- database (GZ-006) ------------------------------------------------------------------


def test_database_url_is_secret(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env)
    settings = load_settings()
    assert "s3cret-db-pass" not in repr(settings)
    assert settings.database_url.get_secret_value() == REQUIRED["DATABASE_URL"]


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://gize_app:pw@db:5432/gize",
        "postgres://gize_app:pw@db/gize",
        "postgresql+asyncpg://gize_app.abcdef:pw@pooler.supabase.com:5432/postgres",
        "postgresql://gize_app:pw@db:5432/gize?ssl=require",
    ],
)
def test_database_url_accepted(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, DATABASE_URL=value)
    assert load_settings().database_url.get_secret_value() == value


@pytest.mark.parametrize(
    "value",
    [
        "mysql://gize_app:pw@db/gize",
        "postgresql://db/gize",  # no user
        "postgresql://gize_app:pw@/gize",  # no host
        "postgresql://gize_app:pw@db:99999/gize",
        "postgresql://gize_app:pw@db",  # no database
        "postgresql://gize_app:pw@db/gize?sslmode=disable",  # unknown option
        "postgresql://gize_app:pw@db/gize?ssl=bogus",
        "postgresql://gize_app:pw@db/gize#x",
    ],
)
def test_database_url_rejected_without_echo(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, DATABASE_URL=value)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert "database_url" in str(exc_info.value)
    assert ":pw@" not in str(exc_info.value)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_database_url_requires_tls_outside_local(
    clean_env: pytest.MonkeyPatch, app_env: str
) -> None:
    _set(clean_env, APP_ENV=app_env, DATABASE_URL="postgresql://gize_app:pw@db/gize")
    with pytest.raises(ConfigError, match="database_url"):
        load_settings()
    _set(
        clean_env,
        APP_ENV=app_env,
        DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=verify-full",
    )
    assert load_settings().database_ssl == "verify-full"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_certificate_verification_outside_local(
    clean_env: pytest.MonkeyPatch, app_env: str
) -> None:
    """Staging verifies like production, so a certificate problem shows up before launch."""
    _set(clean_env, APP_ENV=app_env, DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=require")
    with pytest.raises(ConfigError, match="ssl=verify-full in staging and production"):
        load_settings()


def test_database_root_certificate(clean_env: pytest.MonkeyPatch) -> None:
    import certifi

    _set(
        clean_env,
        DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=verify-full",
        DATABASE_SSL_ROOT_CERT=certifi.where(),
    )
    assert str(load_settings().database_ssl_root_cert) == certifi.where()
    _set(clean_env, DATABASE_SSL_ROOT_CERT="/no/such/ca.crt")
    with pytest.raises(ConfigError, match="database_ssl_root_cert"):
        load_settings()
    _set(
        clean_env,
        DATABASE_URL="postgresql://gize_app:pw@db/gize?ssl=require",
        DATABASE_SSL_ROOT_CERT=certifi.where(),
    )
    with pytest.raises(ConfigError, match="needs ssl=verify-full"):
        load_settings()


def test_database_pool_defaults(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env)
    settings = load_settings()
    assert (settings.database_pool_size, settings.database_max_overflow) == (5, 5)
    _set(clean_env, DATABASE_POOL_SIZE="2", DATABASE_MAX_OVERFLOW="0")
    settings = load_settings()
    assert (settings.database_pool_size, settings.database_max_overflow) == (2, 0)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_POOL_SIZE", "0"),
        ("DATABASE_POOL_SIZE", "51"),
        ("DATABASE_POOL_SIZE", "five"),
        ("DATABASE_MAX_OVERFLOW", "-1"),
        ("DATABASE_MAX_OVERFLOW", "51"),
    ],
)
def test_database_pool_bounds(clean_env: pytest.MonkeyPatch, name: str, value: str) -> None:
    _set(clean_env, **{name: value})
    with pytest.raises(ConfigError, match=name.lower()):
        load_settings()


def test_env_example_loads_as_is(clean_env: pytest.MonkeyPatch) -> None:
    """Copying .env.example gives a valid local configuration; empty values mean unset."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    for line in example.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            clean_env.setenv(name, value)
    settings = load_settings()
    assert settings.sentry_dsn is None
    assert settings.database_ssl_root_cert is None
    assert settings.release is None


def test_client_ip_header_is_off_by_default(clean_env: pytest.MonkeyPatch) -> None:
    """Off: the socket address. Trusting a header no proxy sets would let anyone pick their IP."""
    _set(clean_env)
    assert load_settings().client_ip_header is None
    _set(clean_env, CLIENT_IP_HEADER="CF-Connecting-IP")
    assert load_settings().client_ip_header == "cf-connecting-ip"


@pytest.mark.parametrize("value", ["CF Connecting IP", "x" * 65, "Bad:Header", "X-Ip\\n"])
def test_client_ip_header_must_be_a_header_name(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, CLIENT_IP_HEADER=value)
    with pytest.raises(ConfigError, match="client_ip_header"):
        load_settings()


def test_supabase_url_gives_the_token_issuer_and_key_set(clean_env: pytest.MonkeyPatch) -> None:
    _set(clean_env, SUPABASE_URL="https://abcdefghijklmnopqrst.supabase.co/")
    settings = load_settings()
    assert settings.supabase_url == "https://abcdefghijklmnopqrst.supabase.co"
    assert settings.supabase_auth_issuer == "https://abcdefghijklmnopqrst.supabase.co/auth/v1"
    assert settings.supabase_jwks_url == (
        "https://abcdefghijklmnopqrst.supabase.co/auth/v1/.well-known/jwks.json"
    )
    assert settings.supabase_host == "abcdefghijklmnopqrst.supabase.co"


@pytest.mark.parametrize(
    "value",
    [
        "http://abcdefghijklmnopqrst.supabase.co",  # https only: the outbound client needs it
        "https://user:pw@abcdefghijklmnopqrst.supabase.co",
        "https://abcdefghijklmnopqrst.supabase.co/auth/v1",  # the project URL, not a path
        "https://abcdefghijklmnopqrst.supabase.co?x=1",
        "abcdefghijklmnopqrst.supabase.co",
    ],
)
def test_supabase_url_must_be_the_project_url(clean_env: pytest.MonkeyPatch, value: str) -> None:
    _set(clean_env, SUPABASE_URL=value)
    with pytest.raises(ConfigError, match="supabase_url") as exc_info:
        load_settings()
    assert "pw" not in str(exc_info.value)
