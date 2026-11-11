"""Realistic demo data on staging for partner demos (GZ-074), through the API.

    uv run python -m scripts.demo_seed seed --token-file ~/.gize-drill-token --allow-sms
    uv run python -m scripts.demo_seed finish …   # completes a run that stopped after riders
    uv run python -m scripts.drill_seed remove --token-file ~/.gize-drill-token \\
        --state ~/.gize-demo-seed.json

What it makes, all made up but in Addis Ababa's real places:
- 10 areas, 6 corridors, 6 drivers and 2 companies;
- 10 public routes: 3 launched for the next 1st, each with a driver; 6 forming (some
  ready to launch); 1 draft without prices;
- 1 corporate route, active today with a driver, whose 8 company riders ride from today
  (individual riders can only start on a 1st: these make the overview show riders riding);
- 82 adult riders, 70 of them reserved on the routes, and 15 route requests.

Launching texts the riders it starts, so `--allow-sms` is required. On staging without an
AfroMessage token the texts fail (shown on those routes' launch messages). The phones are
made-up but valid-format numbers: before giving staging an AfroMessage token, remove this
data (or never retry those messages).

Needs an admin's token. Removing it the day it was made stops at the company riders: they
rode today, and a rider can only be deleted once no seat covers today. Remove it a day later.
Every ID created goes into `--state` (default
~/.gize-demo-seed.json), so `drill_seed remove --state …` takes it all away again.
"""

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from gize_api.core.schedule import addis_today
from scripts.drill_seed import (
    ALLOWED_HOSTS,
    STAGING,
    TIMEOUT_SECONDS,
    WARM_UP_SECONDS,
    Api,
    State,
    next_first,
)

STATE_FILE = Path("~/.gize-demo-seed.json")

AREAS = ("Bole", "CMC", "Megenagna", "Piassa", "Ayat", "Summit", "Kazanchis", "Mexico",
         "Sarbet", "Gerji")  # fmt: skip
# (origin, destination, areas passed on the way)
CORRIDORS = (
    ("CMC", "Bole", ("Megenagna",)),
    ("Ayat", "Kazanchis", ("CMC", "Megenagna")),
    ("Summit", "Mexico", ("Bole",)),
    ("Gerji", "Piassa", ("Kazanchis",)),
    ("Megenagna", "Sarbet", ("Bole", "Mexico")),
    ("Bole", "Piassa", ("Kazanchis",)),
)
# code, corridor index, morning leave/arrive, evening leave/arrive, (round trip, one way,
# Saturday) or None (a draft without prices), and what happens to it.
ROUTES: tuple[tuple[str, int, str, str, str, str, tuple[int, int, int | None] | None, str], ...] = (
    ("CMC-BOLE-1", 0, "06:30", "07:15", "17:30", "18:15", (3200, 1800, 600), "launch"),
    ("CMC-BOLE-2", 0, "07:00", "07:45", "18:00", "18:45", (3200, 1800, 600), "ready"),
    ("AYAT-KAZ-1", 1, "06:15", "07:20", "17:15", "18:20", (3600, 2000, None), "launch"),
    ("AYAT-KAZ-2", 1, "06:45", "07:50", "17:45", "18:50", (3600, 2000, None), "forming"),
    ("SUM-MEX-1", 2, "06:20", "07:30", "17:20", "18:30", (3400, 1900, 650), "launch"),
    ("GERJI-PIA-1", 3, "06:40", "07:35", "17:40", "18:35", (3000, 1700, 550), "ready"),
    ("MEG-SAR-1", 4, "06:30", "07:25", "17:30", "18:25", (3300, 1850, None), "forming"),
    ("BOLE-PIA-1", 5, "07:00", "07:50", "18:00", "18:50", (3100, 1750, 600), "forming"),
    ("BOLE-PIA-2", 5, "07:30", "08:20", "18:30", "19:20", (3100, 1750, 600), "forming"),
    ("MEG-SAR-2", 4, "07:15", "08:10", "18:15", "19:10", None, "draft"),
)
# Reserved riders per route: launched ones reach their minimum of 10, "ready" ones too.
RESERVED = {"launch": 10, "ready": 10, "forming": 5, "draft": 0}
SPARE_PER_CORRIDOR = 2  # riders recorded but not placed yet
DRIVERS = (
    ("Dawit Alemu", "Bole, Woreda 03", "AA 3-B12345"),
    ("Tesfaye Bekele", "CMC, Woreda 13", "AA 3-C23456"),
    ("Yohannes Girma", "Ayat, Woreda 12", "AA 2-A34567"),
    ("Abebe Tadesse", "Summit, Woreda 11", "AA 3-B45678"),
    ("Mulugeta Haile", "Gerji, Woreda 08", "AA 2-B56789"),
    ("Solomon Kebede", "Megenagna, Woreda 07", "AA 3-A67890"),
)
COMPANIES = (
    ("Sheba Digital PLC", "Hanna Wolde", "transport@sheba-digital.example"),
    ("Abay Consulting PLC", "Meseret Alemayehu", "office@abay-consulting.example"),
)
GIVEN = ("Selam", "Meron", "Hiwot", "Bethlehem", "Saron", "Liya", "Rahel", "Tigist", "Hana",
         "Mahlet", "Yonas", "Abel", "Dawit", "Henok", "Natnael", "Biniam", "Kaleb", "Robel",
         "Samuel", "Eyob", "Kidist", "Feven", "Ruth", "Helen")  # fmt: skip
FATHERS = ("Tesfaye", "Girma", "Bekele", "Haile", "Alemu", "Tadesse", "Kebede", "Mekonnen",
           "Wolde", "Assefa", "Desta", "Getachew", "Negash", "Worku", "Abebe")  # fmt: skip
REQUESTS_FOR_ROUTES = 8
NEW_AREA_REQUESTS = (("Lebu", "Bole"), ("Jemo", "Mexico"), ("Kotebe", "Piassa"),
                     ("CMC", "Sar Bet Ber"), ("Lafto", "Kazanchis"), ("Tor Hailoch", "Bole"),
                     ("Kality", "Megenagna"))  # fmt: skip
CORPORATE_RIDERS = 8
# The add-on only where every route of the corridor sells Saturdays (else a seat is refused).
SATURDAY_CORRIDORS = frozenset(
    corridor
    for corridor in range(len(CORRIDORS))
    if all(prices is not None and prices[2] is not None
           for _, c, *_, prices, _ in ROUTES if c == corridor)
)  # fmt: skip


def name(n: int) -> str:
    return f"{GIVEN[n % len(GIVEN)]} {FATHERS[(n * 7) % len(FATHERS)]}"


def phone(block: int, n: int) -> str:
    """Made-up, valid-format mobiles in one block per kind (never sent to on purpose)."""
    return f"+2517007{block}{n:04d}"


def seed(api: Api, state: State, path: Path, today: date) -> State:
    def keep() -> None:
        state.save(path)

    body: dict[str, Any]
    area = {}
    for title in AREAS:
        area[title] = api.create("/v1/areas", {"name": title})
        state.areas.append(area[title])
        keep()
    for origin, destination, _ in CORRIDORS:
        body = {"origin_area_id": area[origin], "destination_area_id": area[destination]}
        state.corridors.append(api.create("/v1/corridors", body))
        keep()
    drivers = []
    for n, (full_name, address, plate) in enumerate(DRIVERS):
        body = {"full_name": full_name, "phone": phone(1, n), "address": address, "plate": plate}
        drivers.append(api.create("/v1/drivers", body))
        state.drivers.append(drivers[-1])
        keep()
    companies = []
    for n, (title, contact, email) in enumerate(COMPANIES):
        body = {"name": title, "contact_name": contact, "contact_phone": phone(2, n),
                "contact_email": email}  # fmt: skip
        companies.append(api.create("/v1/companies", body))
        state.companies.append(companies[-1])
        keep()

    routes: list[tuple[str, str, int]] = []  # id, fate, corridor
    for code, corridor, leave, arrive, back, home, prices, fate in ROUTES:
        body = {
            "code": code, "corridor_id": state.corridors[corridor], "route_type": "public",
            "morning_time": leave, "morning_arrival_time": arrive, "return_time": back,
            "return_arrival_time": home, "seats": 14, "launch_minimum": 10,
        }  # fmt: skip
        route = api.create("/v1/routes", body)
        state.routes.append(route)
        keep()
        passed = [area[title] for title in CORRIDORS[corridor][2]]
        api.call("PUT", f"/v1/routes/{route}/areas", {"area_ids": passed})
        if prices is not None:
            round_trip, one_way, saturday = prices
            fares = {"round_trip": round_trip, "one_way": one_way, "saturday": saturday}
            api.call("PUT", f"/v1/routes/{route}/prices", fares)
            api.call("PUT", f"/v1/routes/{route}/status", {"status": "forming"})
        routes.append((route, fate, corridor))

    corporate = api.create(
        "/v1/routes",
        {"code": "SHEBA-STAFF-1", "corridor_id": state.corridors[0], "route_type": "corporate",
         "company_id": companies[0], "morning_time": "07:30", "morning_arrival_time": "08:15",
         "return_time": "17:30", "return_arrival_time": "18:15", "seats": 12},
    )  # fmt: skip
    state.routes.append(corporate)
    keep()
    api.call("PUT", f"/v1/routes/{corporate}/status", {"status": "active"})

    waiting: dict[int, list[str]] = {corridor: [] for corridor in range(len(CORRIDORS))}
    n = 0
    for corridor, (origin, destination, _) in enumerate(CORRIDORS):
        needed = sum(RESERVED[fate] for _, c, *_, fate in ROUTES if c == corridor)
        for _ in range(needed + SPARE_PER_CORRIDOR):
            body = {"full_name": name(n), "phone": phone(3, n), "home_area_id": area[origin],
                    "destination_area_id": area[destination],
                    "plan": ("round_trip", "round_trip", "round_trip", "morning", "evening")[n % 5],
                    "saturday_addon": corridor in SATURDAY_CORRIDORS and n % 4 == 0}  # fmt: skip
            rider = api.create("/v1/riders", body)
            waiting[corridor].append(rider)
            state.riders.append(rider)
            keep()
            n += 1
    for route, fate, corridor in routes:
        for rider in [waiting[corridor].pop() for _ in range(RESERVED[fate])]:
            api.call("PUT", f"/v1/riders/{rider}/seat", {"route_id": route}, keyed=True)
            state.seated.append(rider)
            keep()
    for n in range(CORPORATE_RIDERS):
        body = {"rider_type": "corporate", "company_id": companies[0], "full_name": name(n + 40),
                "phone": phone(4, n), "home_area_id": area["CMC"]}  # fmt: skip
        # No destination, plan or Saturday: company riders ride to the route's B.
        rider = api.create("/v1/riders", body)
        state.riders.append(rider)
        keep()
        start = today.isoformat()
        api.call("PUT", f"/v1/riders/{rider}/seat", {"route_id": corporate, "start_date": start},
                 keyed=True)  # fmt: skip
        state.seated.append(rider)
        keep()
    return finish(api, state, path, today)


def finish(api: Api, state: State, path: Path, today: date) -> State:
    """The last steps, from the state file alone: launches and drivers, then the requests
    (a route request needs a running route). `finish` completes a run that stopped here."""
    public = state.routes[: len(ROUTES)]
    corporate = state.routes[len(ROUTES)]
    area = dict(zip(AREAS, state.areas, strict=True))
    drivers = state.drivers
    launched = [(route, spec[1]) for route, spec in zip(public, ROUTES, strict=True)
                if spec[7] == "launch"]  # fmt: skip

    def keep() -> None:
        state.save(path)

    # Launched first: a route request needs a running (active or paused) route.
    launch_date = next_first(today).isoformat()
    for n, (route, _) in enumerate(launched):
        launch = {"status": "active", "launch_date": launch_date}
        api.call("PUT", f"/v1/routes/{route}/status", launch)
        api.call("PUT", f"/v1/routes/{route}/driver", {"driver_id": drivers[n]})
    api.call("PUT", f"/v1/routes/{corporate}/driver", {"driver_id": drivers[len(launched)]})
    for n in range(REQUESTS_FOR_ROUTES):
        route, _ = launched[n % len(launched)]
        body = {"kind": "route", "route_id": route, "rider_type": "adult",
                "rider_name": name(n + 60), "contact_name": name(n + 60),
                "contact_phone": phone(5, n)}  # fmt: skip
        state.requests.append(api.create("/v1/route-requests", body))
        keep()
    for n, (origin, destination) in enumerate(NEW_AREA_REQUESTS):
        known = {"from_area_id": area[origin]} if origin in area else {"from_area_name": origin}
        to = (
            {"to_area_id": area[destination]}
            if destination in area
            else {"to_area_name": destination}
        )
        asker = name(n + 70)
        body = {"kind": "new_areas", "rider_type": "adult", "rider_name": asker,
                "contact_name": asker, "contact_phone": phone(6, n), **known, **to}  # fmt: skip
        state.requests.append(api.create("/v1/route-requests", body))
        keep()

    return state


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["seed", "finish"])
    parser.add_argument("--base-url", default=STAGING)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--allow-sms", action="store_true", help="launches text riders")
    parser.add_argument("--per-minute", type=int, default=100)
    args = parser.parse_args(argv)
    if not args.base_url.startswith(ALLOWED_HOSTS):
        parser.error(f"demo data goes on staging or a local copy only, not {args.base_url}")
    if not args.allow_sms:
        parser.error("the launches text their riders (made-up, valid-format numbers): add "
                     "--allow-sms, and only where SMS is off")  # fmt: skip
    path = args.state.expanduser()
    state = State.load(path)
    if args.action == "seed" and any(getattr(state, kind) for kind in vars(state)):
        raise SystemExit(f"{path} lists earlier demo data: remove it first")
    if args.action == "finish" and (len(state.routes) != len(ROUTES) + 1 or state.requests):
        raise SystemExit(f"{path} isn't a run that stopped before its launches and requests")
    token = Path(args.token_file).expanduser().read_text().strip()
    with httpx.Client(
        base_url=args.base_url, timeout=TIMEOUT_SECONDS, transport=transport
    ) as client:
        client.get("/health/ready", timeout=WARM_UP_SECONDS)
        api = Api(client, token, per_minute=args.per_minute)
        step = seed if args.action == "seed" else finish
        step(api, state, path, addis_today())
    counts = ", ".join(f"{len(ids)} {kind}" for kind, ids in vars(state).items())
    sys.stdout.write(f"demo: done ({counts} in {path})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
