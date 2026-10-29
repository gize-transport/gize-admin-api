"""Write openapi.json, the API contract with gize-admin-web (docs/api-contract.md).

    uv run python scripts/export_openapi.py          # regenerate; commit it with the change
    uv run python scripts/export_openapi.py --check  # CI: fail if it is out of date

The document is deterministic (sorted keys, two-space indent, a final newline) and never
depends on the environment: the app is built from fixed placeholder settings.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from gize_api.core.config import Environment, Settings
from gize_api.main import create_app

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"
REGENERATE = "uv run python scripts/export_openapi.py"


class _FixedSettings(Settings):
    """Placeholder settings from arguments only: no environment, no .env file."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def render_contract() -> str:
    # The contract must not vary by environment: if a route ever became environment-
    # conditional, production's API would differ from this document. Don't do that.
    settings = _FixedSettings(
        app_env=Environment.TEST,
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
        database_url=SecretStr("postgresql://gize_app:unused@127.0.0.1:1/gize"),
        supabase_url="https://project-ref.supabase.co",
    )
    schema = create_app(settings).openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    parser.add_argument("--output", type=Path, default=CONTRACT_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    contract = render_contract()
    if args.check:
        current = args.output.read_text() if args.output.exists() else None
        if current != contract:
            print(
                f"openapi.json is out of date: run `{REGENERATE}` and commit it",
                file=sys.stderr,
            )
            return 1
        return 0
    args.output.write_text(contract)
    return 0


if __name__ == "__main__":
    sys.exit(main())
