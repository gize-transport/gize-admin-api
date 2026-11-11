"""Sign a staff member in to staging's Supabase and keep the token for a drill (GZ-063).

    uv run python -m scripts.drill_token

Run it in your own terminal: it asks for the email and the password (not echoed), and for
the project's publishable key (public: Supabase → Project Settings → API Keys) unless
SUPABASE_PUBLISHABLE_KEY is set. The access token (valid about an hour) is written to
~/.gize-drill-token, readable by you only, for scripts/load_drill.py. Neither the password
nor the token is ever printed. Delete the file after the drill.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

import httpx

STAGING_SUPABASE = "https://pahktmbnbyulqkhdruci.supabase.co"
TOKEN_FILE = Path("~/.gize-drill-token")
TIMEOUT_SECONDS = 15.0


def sign_in(
    supabase_url: str,
    key: str,
    email: str,
    password: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, int]:
    """The access token and its lifetime in seconds, or SystemExit with Supabase's reason."""
    with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
        response = client.post(
            f"{supabase_url}/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": key},
            json={"email": email, "password": password},
        )
    if not response.is_success:
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        reason = body.get("error_description") or body.get("msg") or "sign-in refused"
        raise SystemExit(f"Supabase answered {response.status_code}: {reason}")
    data = response.json()
    return str(data["access_token"]), int(data.get("expires_in", 3600))


def save(token: str, path: Path) -> Path:
    """Write the token readable by its owner only (created that way: never world-readable)."""
    target = path.expanduser()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(token)
    target.chmod(0o600)
    return target


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--supabase-url", default=STAGING_SUPABASE)
    parser.add_argument("--output", type=Path, default=TOKEN_FILE)
    args = parser.parse_args(argv)
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or input("Publishable key: ").strip()
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    token, lifetime = sign_in(args.supabase_url, key, email, password, transport)
    target = save(token, args.output)
    sys.stdout.write(f"Saved to {target} (valid about {lifetime // 60} minutes).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
