"""Made-up data for a drill on staging, through the API, and its removal (GZ-063).

    uv run python -m scripts.drill_seed seed --token-file ~/.gize-drill-token [--launch 5]
    uv run python -m scripts.drill_seed remove --token-file ~/.gize-drill-token

`seed` creates about first-six-months size, all named "Drill …" with made-up phones:
20 areas, 10 corridors, 30 forming public routes (prices set), 600 riders, 390 of them
reserved on the routes, and 50 route requests. `--launch N --allow-sms` launches N routes on
the next 1st: each launch texts its riders (made-up but valid-format numbers), so only on a
staging without an AfroMessage token (the sends fail, which is the SMS drill).

Needs an **admin's** token (prices and deletes are admin-only). Runs only against staging or
a local copy. `remove` before the launch date: seats that have started are kept to a month's
end, and their riders can't be deleted until then. Every ID created is written to `--state`
as it is made.

`remove` undoes exactly what the state file lists, in reverse: seats ended, riders,
requests, routes, corridors and areas deleted (soft deletes, audited, as staff would).

Requests are paced under the staff limit (120 a minute per account). Stops at the first
unexpected answer, naming the endpoint, the status and the error code; never prints the
token. Not part of the API: a tool for drills.
"""

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from gize_api.core.schedule import addis_today

STAGING = "https://gize-api-staging.onrender.com"
ALLOWED_HOSTS = ("https://gize-api-staging.onrender.com", "http://localhost", "http://127.0.0.1",
                 "http://test")  # fmt: skip
WARM_UP_SECONDS = 120.0
STATE_FILE = Path("~/.gize-drill-seed.json")
TIMEOUT_SECONDS = 30.0
PER_MINUTE = 100  # under the staff limit of 120
AREAS, CORRIDORS, ROUTES_PER_CORRIDOR, RIDERS, PER_ROUTE, REQUESTS = 20, 10, 3, 600, 13, 50


@dataclass
class State:
    areas: list[str] = field(default_factory=list)
    corridors: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    riders: list[str] = field(default_factory=list)
    seated: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)  # demo data (scripts/demo_seed.py)
    companies: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path) -> None:
        """Atomically: an interrupt leaves the old file or the new one, never half of one."""
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(asdict(self), indent=1) + "\n")
        partial.replace(path)


class Api:
    """One staff member's calls, paced; any answer but the expected ones stops the run."""

    def __init__(
        self,
        client: httpx.Client,
        token: str,
        per_minute: int = PER_MINUTE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.gap = 60.0 / per_minute
        self.sleep = sleep
        self.last = 0.0

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        ok: tuple[int, ...] = (200, 201, 204),
        keyed: bool = False,
    ) -> httpx.Response:
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self.last = time.monotonic()
        headers = dict(self.headers)
        if keyed:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code not in ok:
            try:
                code = response.json().get("code")
            except ValueError:  # an HTML error page from the proxy
                code = None
            raise SystemExit(f"{method} {path}: {response.status_code} {code}")
        return response

    def create(self, path: str, body: dict[str, Any]) -> str:
        return str(self.call("POST", path, body, keyed=True).json()["id"])


def next_first(today: date) -> date:
    return date(today.year + today.month // 12, today.month % 12 + 1, 1)


def seed(api: Api, state: State, path: Path, launch: int, today: date) -> State:
    def keep() -> None:
        state.save(path)

    for n in range(AREAS):
        state.areas.append(api.create("/v1/areas", {"name": f"Drill Area {n + 1:02d}"}))
        keep()
    pairs = [(state.areas[2 * n], state.areas[2 * n + 1]) for n in range(CORRIDORS)]
    for origin, destination in pairs:
        body = {"origin_area_id": origin, "destination_area_id": destination}
        state.corridors.append(api.create("/v1/corridors", body))
        keep()
    for n in range(CORRIDORS * ROUTES_PER_CORRIDOR):
        hour = 6 + n % 3
        route = api.create(
            "/v1/routes",
            {
                "code": f"DRILL-{n + 1:02d}",
                "corridor_id": state.corridors[n // ROUTES_PER_CORRIDOR],
                "route_type": "public",
                "morning_time": f"{hour:02d}:30",
                "morning_arrival_time": f"{hour + 1:02d}:15",
                "return_time": f"{hour + 11:02d}:30",
                "return_arrival_time": f"{hour + 12:02d}:15",
                "seats": 14,
                "launch_minimum": 10,
            },
        )
        state.routes.append(route)
        keep()
        api.call("PUT", f"/v1/routes/{route}/prices", {"round_trip": 3200, "one_way": 1800})
        api.call("PUT", f"/v1/routes/{route}/status", {"status": "forming"})
    for n in range(RIDERS):
        corridor = n % CORRIDORS
        body = {
            "full_name": f"Drill Rider {n + 1:03d}",
            "phone": f"+25170063{n:04d}",
            "home_area_id": pairs[corridor][0],
            "destination_area_id": pairs[corridor][1],
            "plan": "round_trip",
        }
        state.riders.append(api.create("/v1/riders", body))
        keep()
    for n, route in enumerate(state.routes):
        corridor = n // ROUTES_PER_CORRIDOR
        on_corridor = [r for i, r in enumerate(state.riders) if i % CORRIDORS == corridor]
        start = (n % ROUTES_PER_CORRIDOR) * PER_ROUTE
        for rider in on_corridor[start : start + PER_ROUTE]:
            api.call("PUT", f"/v1/riders/{rider}/seat", {"route_id": route}, keyed=True)
            state.seated.append(rider)
            keep()
    for n in range(REQUESTS):
        body = {
            "kind": "new_areas",
            "rider_type": "adult",
            "rider_name": f"Drill Asker {n + 1:02d}",
            "contact_name": f"Drill Asker {n + 1:02d}",
            "contact_phone": f"+25170064{n:04d}",
            "from_area_name": f"Drill Place {n % 7}",
            "to_area_name": f"Drill Place {n % 7 + 7}",
        }
        state.requests.append(api.create("/v1/route-requests", body))
        keep()
    launch_date = next_first(today).isoformat()
    for route in state.routes[:launch]:
        api.call(
            "PUT", f"/v1/routes/{route}/status", {"status": "active", "launch_date": launch_date}
        )
    return state


def remove(api: Api, state: State, path: Path) -> State:
    """Undo, newest first; what is already gone (404) is skipped, so it can run again."""
    while state.seated:
        rider = state.seated[-1]
        api.call("POST", f"/v1/riders/{rider}/seat/end", {}, ok=(200, 404, 409), keyed=True)
        state.seated.pop()
        state.save(path)
    for kind, prefix in (
        ("riders", "/v1/riders"),
        ("requests", "/v1/route-requests"),
        ("routes", "/v1/routes"),
        ("corridors", "/v1/corridors"),
        ("areas", "/v1/areas"),
        ("drivers", "/v1/drivers"),
        ("companies", "/v1/companies"),
    ):
        ids: list[str] = getattr(state, kind)
        while ids:
            api.call("DELETE", f"{prefix}/{ids[-1]}", ok=(204, 404))
            ids.pop()
            state.save(path)
    return state


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["seed", "remove"])
    parser.add_argument("--base-url", default=STAGING)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--launch", type=int, default=0, help="routes to launch (texts riders)")
    parser.add_argument("--allow-sms", action="store_true", help="needed with --launch")
    parser.add_argument("--per-minute", type=int, default=PER_MINUTE)
    args = parser.parse_args(argv)
    if not args.base_url.startswith(ALLOWED_HOSTS):
        parser.error(f"drills run on staging or a local copy only, not {args.base_url}")
    if args.launch and not args.allow_sms:
        parser.error("--launch texts every rider it starts (made-up but valid numbers): add "
                     "--allow-sms, and only where SMS is off")  # fmt: skip
    token = Path(args.token_file).expanduser().read_text().strip()
    path = args.state.expanduser()
    state = State.load(path)
    if args.action == "seed" and any(asdict(state).values()):
        raise SystemExit(f"{path} lists earlier drill data: remove it first")
    with httpx.Client(
        base_url=args.base_url, timeout=TIMEOUT_SECONDS, transport=transport
    ) as client:
        # Wake staging first: a create whose answer is lost to a slow wake-up would leave a
        # row the state file never lists.
        client.get("/health/ready", timeout=WARM_UP_SECONDS)
        api = Api(client, token, per_minute=args.per_minute)
        if args.action == "seed":
            seed(api, state, path, args.launch, addis_today())  # the API's own "today"
        else:
            remove(api, state, path)
    counts = ", ".join(f"{len(ids)} {kind}" for kind, ids in asdict(state).items())
    sys.stdout.write(f"{args.action}: done ({counts} in {path})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
