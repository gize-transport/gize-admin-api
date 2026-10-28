"""GZ-006: settings for the migration run (migrations/env.py), validated like the app's."""

import ssl
from pathlib import Path

import certifi
import pytest
from alembic.runtime.environment import NameFilterType

from gize_api.core.config import ConfigError
from gize_api.core.migrations import (
    create_migration_engine,
    include_name,
    load_migration_settings,
)

URL = "postgresql://gize_migrator:secret-pw@db.example.com:5432/postgres"


def _load(**environ: str) -> object:
    return load_migration_settings({}, environ)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"APP_ENV": "test"}, "set MIGRATOR_DATABASE_URL"),
        (
            {"APP_ENV": "test", "MIGRATOR_DATABASE_URL": URL.replace("gize_migrator", "gize_app")},
            "must connect as gize_migrator",
        ),
        (
            {"APP_ENV": "test", "MIGRATOR_DATABASE_URL": URL + "?sslmode=disable"},
            "must be postgresql://",
        ),
        ({"MIGRATOR_DATABASE_URL": URL}, "set APP_ENV"),
        ({"APP_ENV": "prod", "MIGRATOR_DATABASE_URL": URL}, "set APP_ENV"),
        ({"APP_ENV": "staging", "MIGRATOR_DATABASE_URL": URL}, "ssl=verify-full in staging"),
        (
            {"APP_ENV": "production", "MIGRATOR_DATABASE_URL": URL + "?ssl=require"},
            "ssl=verify-full in staging and production",
        ),
        (
            {
                "APP_ENV": "test",
                "MIGRATOR_DATABASE_URL": URL,
                "DATABASE_SSL_ROOT_CERT": "/no/such/file.crt",
            },
            "DATABASE_SSL_ROOT_CERT must be a readable file",
        ),
        (
            {
                "APP_ENV": "test",
                "MIGRATOR_DATABASE_URL": URL + "?ssl=require",
                "DATABASE_SSL_ROOT_CERT": certifi.where(),
            },
            "needs ssl=verify-full when DATABASE_SSL_ROOT_CERT is set",
        ),
    ],
)
def test_invalid_settings_are_refused_without_echoing_secrets(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message) as exc_info:
        _load(**environ)
    assert "secret-pw" not in str(exc_info.value)


def test_attributes_take_precedence_over_the_environment() -> None:
    settings = load_migration_settings(
        {"database_url": URL, "app_env": "test"},
        {"MIGRATOR_DATABASE_URL": "postgresql://other:pw@h/d", "APP_ENV": "nonsense"},
    )
    assert settings.url == URL


async def test_engine_hides_parameters_and_verifies_tls_with_the_root_certificate() -> None:
    settings = load_migration_settings(
        {},
        {
            "APP_ENV": "production",
            "MIGRATOR_DATABASE_URL": URL + "?ssl=verify-full",
            "DATABASE_SSL_ROOT_CERT": certifi.where(),
        },
    )
    assert settings.ssl_root_cert == Path(certifi.where())
    engine = create_migration_engine(settings)
    try:
        assert engine.sync_engine.hide_parameters is True
        assert engine.url.query == {}  # ssl goes to the driver, not the URL
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("name", "type_", "included"),
    [
        ("gize", "schema", True),
        (None, "schema", False),  # the default schema (public)
        ("auth", "schema", False),
        ("anything", "table", True),  # tables are filtered by their schema
    ],
)
def test_alembic_compares_only_the_gize_schema(
    name: str | None, type_: NameFilterType, included: bool
) -> None:
    assert include_name(name, type_, {}) is included


def test_gize_must_not_be_the_default_schema() -> None:
    from gize_api.core.migrations import check_default_schema

    check_default_schema("public")
    with pytest.raises(ConfigError, match="search_path must not start with gize"):
        check_default_schema("gize")


def test_verify_full_context_trusts_only_the_given_root(tmp_path: Path) -> None:
    from gize_api.core.db import ssl_connect_argument

    context = ssl_connect_argument("postgresql://u:p@h/d?ssl=verify-full", Path(certifi.where()))[
        "ssl"
    ]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.get_ca_certs()
