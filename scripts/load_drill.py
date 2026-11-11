"""A light load on staging at the expected peak (GZ-063): staff using the portal, visitors
browsing the website, a few people asking for a phone code.

    uv run python -m scripts.load_drill --minutes 10 --token-file ~/.gize-drill-token

- Staff: `--staff` portal users on one account (scripts/drill_token.py signs it in), each
  making a request every `--staff-interval` seconds (jittered): lists, the overview, one
  route. One account's limit is 120 a minute: 5 users every 3 s stay under it.
- Public: visitors arriving at `--public-rate` a second (Poisson), each reading the areas or
  searching the routes between two of them, as the website does.
  The public read limit is 60 a minute per address, and this runs from one.
- Phone codes: `--signups` requests spread over the run, only with `--allow-sms` (they text
  a made-up but valid-format number when SMS is on). Off by default.
- First a warm-up: staging's free plan sleeps when idle.

Prints, per endpoint, the count by status and the p50/p95/max latency (over every request,
refused or failed ones included; status 0: no answer); `--output` also writes them as JSON.
Runs only against staging or a local copy. Never prints the token. Not part of the API: a
tool for drills.
"""

import argparse
import asyncio
import json
import math
import random
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

STAGING = "https://gize-api-staging.onrender.com"
# Where a drill may run: staging, or a local copy (tests use http://test).
ALLOWED_HOSTS = ("https://gize-api-staging.onrender.com", "http://localhost", "http://127.0.0.1",
                 "http://test")  # fmt: skip
REQUEST_TIMEOUT_SECONDS = 30.0
WARM_UP_TIMEOUT_SECONDS = 120.0
STAFF_PATHS = (
    "/v1/overview",
    "/v1/routes?limit=25",
    "/v1/riders?limit=25",
    "/v1/route-requests?limit=25",
    "/v1/route-requests/summary",
    "/v1/areas?limit=25",
    "/v1/drivers?limit=25",
    "/v1/staff/me",
)
PUBLIC_AREAS = "/v1/public/areas?limit=100"
PUBLIC_ROUTES = "/v1/public/routes"  # with from_area_id and to_area_id, as the website asks
SIGNUP_PATH = "/v1/verification/request"
# Made up, but a valid-format mobile: someone may own it. Phone codes are only sent with
# --allow-sms, and only make sense where SMS is off or goes to a test provider.
SIGNUP_PHONE = "0911000063"
ROUTE_DETAIL = "/v1/routes/{route_id}"

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class Results:
    """Each request's endpoint (method and path pattern), status (0: no answer) and time."""

    samples: dict[str, list[tuple[int, float]]] = field(default_factory=lambda: defaultdict(list))

    def record(self, endpoint: str, status: int, seconds: float) -> None:
        self.samples[endpoint].append((status, seconds))


def percentile(values: list[float], share: float) -> float:
    """Nearest-rank percentile (`share` in 0..1) of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return ordered[rank - 1]


def summary(results: Results) -> dict[str, dict[str, Any]]:
    """Per endpoint: count, statuses and p50/p95/max in milliseconds."""
    report = {}
    for endpoint, samples in sorted(results.samples.items()):
        times = [seconds * 1000 for _, seconds in samples]
        report[endpoint] = {
            "count": len(samples),
            "statuses": dict(sorted(Counter(str(status) for status, _ in samples).items())),
            "p50_ms": round(percentile(times, 0.50)),
            "p95_ms": round(percentile(times, 0.95)),
            "max_ms": round(max(times)),
        }
    return report


async def _call(
    client: httpx.AsyncClient,
    results: Results,
    endpoint: str,
    method: str,
    path: str,
    **kwargs: Any,  # noqa: ANN401 - httpx's request options
) -> httpx.Response | None:
    started = time.monotonic()
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError:
        results.record(endpoint, 0, time.monotonic() - started)
        return None
    results.record(endpoint, response.status_code, time.monotonic() - started)
    return response


def _ids(response: httpx.Response) -> list[str]:
    """The IDs on a page, or none if the body isn't one (an error page from a proxy)."""
    try:
        return [str(item["id"]) for item in response.json().get("items", [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return []


def _endpoint(method: str, path: str) -> str:
    return f"{method} {path.split('?', 1)[0]}"


async def staff_user(
    client: httpx.AsyncClient,
    token: str,
    *,
    until: float,
    interval: float,
    results: Results,
    rng: random.Random,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """One portal user: a request every `interval` seconds (±50%), now and then a route."""
    headers = {"Authorization": f"Bearer {token}"}
    route_ids: list[str] = []
    while time.monotonic() < until:
        if route_ids and rng.random() < 0.2:
            path = ROUTE_DETAIL.format(route_id=rng.choice(route_ids))
            endpoint = f"GET {ROUTE_DETAIL}"
        else:
            path = rng.choice(STAFF_PATHS)
            endpoint = _endpoint("GET", path)
        response = await _call(client, results, endpoint, "GET", path, headers=headers)
        if response is not None and path.startswith("/v1/routes?") and response.is_success:
            route_ids = _ids(response)
        await sleep(interval * rng.uniform(0.5, 1.5))


async def public_visitors(
    client: httpx.AsyncClient,
    *,
    until: float,
    rate: float,
    results: Results,
    rng: random.Random,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Visitors arriving at `rate` a second (exponential gaps): half read the areas, half
    search the routes between two of them (once the areas are known)."""
    area_ids: list[str] = []
    pending: set[asyncio.Task[None]] = set()

    async def visit() -> None:
        if len(area_ids) >= 2 and rng.random() < 0.5:
            origin, destination = rng.sample(area_ids, 2)
            params = {"from_area_id": origin, "to_area_id": destination}
            endpoint = _endpoint("GET", PUBLIC_ROUTES)
            await _call(client, results, endpoint, "GET", PUBLIC_ROUTES, params=params)
            return
        response = await _call(client, results, _endpoint("GET", PUBLIC_AREAS), "GET", PUBLIC_AREAS)
        if response is not None and response.is_success:
            area_ids[:] = _ids(response)

    while time.monotonic() < until:
        task = asyncio.ensure_future(visit())
        pending.add(task)
        task.add_done_callback(pending.discard)
        await sleep(rng.expovariate(rate))
    await asyncio.gather(*pending)


async def signups(
    client: httpx.AsyncClient,
    *,
    count: int,
    over: float,
    results: Results,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """`count` phone-code requests spread evenly over `over` seconds."""
    for _ in range(count):
        await sleep(over / (count + 1))
        await _call(
            client,
            results,
            _endpoint("POST", SIGNUP_PATH),
            "POST",
            SIGNUP_PATH,
            json={"phone": SIGNUP_PHONE},
            headers={"Idempotency-Key": str(uuid.uuid4())},  # one per request, as the site
        )


async def run(args: argparse.Namespace, client: httpx.AsyncClient, token: str | None) -> Results:
    results = Results()
    await _call(client, results, "warm-up", "GET", "/health/ready", timeout=WARM_UP_TIMEOUT_SECONDS)
    seconds = args.minutes * 60
    until = time.monotonic() + seconds
    rng = random.Random(args.seed)  # noqa: S311 - spacing requests out, not security
    work = [
        public_visitors(client, until=until, rate=args.public_rate, results=results, rng=rng),
        signups(client, count=args.signups, over=seconds, results=results),
    ]
    if token is not None:
        work += [
            staff_user(
                client,
                token,
                until=until,
                interval=args.staff_interval,
                results=results,
                rng=random.Random(args.seed + n),  # noqa: S311 - spacing, not security
            )
            for n in range(args.staff)
        ]
    await asyncio.gather(*work)
    return results


def _table(report: dict[str, dict[str, Any]]) -> str:
    lines = [f"{'endpoint':42} {'count':>6} {'p50':>6} {'p95':>6} {'max':>6}  statuses"]
    for endpoint, row in report.items():
        statuses = " ".join(f"{code}:{n}" for code, n in row["statuses"].items())
        lines.append(
            f"{endpoint:42} {row['count']:>6} {row['p50_ms']:>6} {row['p95_ms']:>6} "
            f"{row['max_ms']:>6}  {statuses}"
        )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=STAGING)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--staff", type=int, default=5)
    parser.add_argument("--staff-interval", type=float, default=3.0)
    parser.add_argument("--public-rate", type=float, default=0.8)
    parser.add_argument("--signups", type=int, default=0, help="phone-code requests (texts)")
    parser.add_argument("--allow-sms", action="store_true", help="needed with --signups")
    parser.add_argument("--token-file", default=None, help="from scripts/drill_token.py")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=63)
    return parser


def main(argv: list[str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.base_url.startswith(ALLOWED_HOSTS):
        parser.error(f"drills run on staging or a local copy only, not {args.base_url}")
    if args.signups and not args.allow_sms:
        parser.error("--signups asks for phone codes, which text a real number: add --allow-sms")
    token = None
    if args.token_file is not None:
        token = Path(args.token_file).expanduser().read_text().strip()

    async def go() -> Results:
        async with httpx.AsyncClient(
            base_url=args.base_url, timeout=REQUEST_TIMEOUT_SECONDS, transport=transport
        ) as client:
            return await run(args, client, token)

    report = summary(asyncio.run(go()))
    sys.stdout.write(_table(report))
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
