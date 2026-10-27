"""GZ-003: settings load from the environment and fail fast without leaking values."""

import pytest
from pydantic import SecretStr

from gize_api.core.config import ConfigError, Environment, Settings, load_settings

REQUIRED = {
    "APP_ENV": "test",
    "REDIS_URL": "redis://:s3cret-redis-pass@127.0.0.1:6379/0",
}


def _set(env: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in {**REQUIRED, **values}.items():
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
