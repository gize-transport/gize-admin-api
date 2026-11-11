"""GZ-063: the drill tools (scripts/load_drill.py, scripts/drill_token.py), against fake
servers: nothing here reaches staging or Supabase."""

import json
import random
import stat
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scripts import drill_seed, drill_token, load_drill
from scripts.load_drill import Results, percentile, summary


def test_percentiles_are_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]
    assert (percentile(values, 0.5), percentile(values, 0.95)) == (50.0, 95.0)
    assert percentile([7.0], 0.95) == 7.0


def test_the_summary_counts_statuses_and_times_per_endpoint() -> None:
    results = Results()
    for status, seconds in ((200, 0.1), (200, 0.3), (429, 0.05), (0, 30.0)):
        results.record("GET /v1/overview", status, seconds)
    assert summary(results) == {
        "GET /v1/overview": {
            "count": 4,
            "statuses": {"0": 1, "200": 2, "429": 1},
            "p50_ms": 100,
            "p95_ms": 30000,
            "max_ms": 30000,
        }
    }


async def _no_wait(_: float) -> None:
    return None


def _routes_page() -> dict[str, list[dict[str, str]]]:
    return {"items": [{"id": "2d8b3f4a-6e5c-4b7a-9c0d-3e4f5a6b7c01"}]}


async def test_a_staff_user_signs_every_request_and_opens_listed_routes() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/routes":
            return httpx.Response(200, json=_routes_page())
        return httpx.Response(200, json={})

    results = Results()
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        stop = time.monotonic() + 0.2
        await load_drill.staff_user(
            client, "tok", until=stop, interval=0.0, results=results,
            rng=random.Random(1), sleep=_no_wait,  # noqa: S311
        )  # fmt: skip
    assert seen
    assert {auth for _, auth in seen} == {"Bearer tok"}
    assert {path for path, _ in seen} <= {p.split("?")[0] for p in load_drill.STAFF_PATHS} | {
        "/v1/routes/2d8b3f4a-6e5c-4b7a-9c0d-3e4f5a6b7c01"
    }
    assert "GET /v1/routes/{route_id}" in results.samples  # by pattern, never the raw ID


async def test_visitors_search_routes_between_two_listed_areas() -> None:
    areas = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]
    searches: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        if request.url.path == "/v1/public/areas":
            return httpx.Response(200, json={"items": areas})
        params = request.url.params
        searches.append((params["from_area_id"], params["to_area_id"]))
        return httpx.Response(200, json={"items": []})

    results = Results()
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await load_drill.public_visitors(
            client, until=time.monotonic() + 0.1, rate=1000.0, results=results,
            rng=random.Random(2), sleep=_no_wait,  # noqa: S311
        )  # fmt: skip
    assert results.samples["GET /v1/public/areas"]
    assert searches
    for origin, destination in searches:
        assert origin != destination
        assert {origin, destination} <= {"a1", "a2", "a3"}


async def test_signups_send_no_token_and_unanswered_counts_as_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        raise httpx.ConnectError("down")

    results = Results()
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await load_drill.signups(client, count=2, over=1.0, results=results, sleep=_no_wait)
    assert [status for status, _ in results.samples["POST /v1/verification/request"]] == [0, 0]


def test_a_whole_run_warms_up_then_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = tmp_path / "token"
    token.write_text("secret-token\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/routes":
            return httpx.Response(200, json=_routes_page())
        return httpx.Response(401 if request.url.path == "/v1/overview" else 200, json={})

    output = tmp_path / "report.json"
    argv = ["--base-url", "http://test", "--minutes", "0.002", "--staff", "2", "--staff-interval",
            "0.01", "--public-rate", "200", "--signups", "1", "--token-file", str(token),
            "--output", str(output)]  # fmt: skip
    assert load_drill.main(argv, transport=httpx.MockTransport(handler)) == 0
    printed = capsys.readouterr().out
    report = json.loads(output.read_text())
    assert report["warm-up"]["statuses"] == {"200": 1}
    assert "POST /v1/verification/request" in report
    assert "secret-token" not in printed + output.read_text()


def test_the_token_is_saved_for_its_owner_only(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["grant_type"] == "password"
        assert request.headers["apikey"] == "pub"
        assert json.loads(request.content) == {"email": "a@example.com", "password": "pw"}
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    token, lifetime = drill_token.sign_in(
        "http://supabase.test", "pub", "a@example.com", "pw", httpx.MockTransport(handler)
    )
    target = drill_token.save(token, tmp_path / "token")
    assert (target.read_text(), lifetime) == ("tok", 3600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(400, json={"error_description": "Invalid login credentials"}),
         "Invalid login credentials"),
        (httpx.Response(502, text="bad gateway"), "sign-in refused"),
    ],
)  # fmt: skip
def test_a_refused_sign_in_says_why_without_the_password(
    response: httpx.Response, reason: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        drill_token.sign_in(
            "http://supabase.test", "pub", "a@example.com", "hunter2",
            httpx.MockTransport(lambda _: response),
        )  # fmt: skip
    assert reason in str(refused.value)
    assert "hunter2" not in str(refused.value)


def test_the_command_prompts_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pub")
    monkeypatch.setattr("builtins.input", lambda _: "a@example.com")
    monkeypatch.setattr("getpass.getpass", lambda _: "pw")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    target = tmp_path / "token"
    assert drill_token.main(["--output", str(target)], transport=transport) == 0
    assert target.read_text() == "tok"
    assert "tok" not in capsys.readouterr().out.replace(str(target), "")


# --- drill_seed ----------------------------------------------------------------------


class FakeApi:
    """Answers the calls drill_seed makes, recording them; nothing reaches staging."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.gone: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        keyed = "idempotency-key" in request.headers
        assert request.headers["authorization"] == "Bearer tok"
        self.calls.append((request.method, request.url.path, keyed))
        if request.method == "POST" and request.url.path.count("/") == 2:
            return httpx.Response(201, json={"id": f"id-{len(self.calls)}"})
        if request.method == "DELETE":
            if request.url.path in self.gone:
                return httpx.Response(404, json={"code": "NOT_FOUND"})
            self.gone.add(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json={})


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("AREAS", 4), ("CORRIDORS", 2), ("ROUTES_PER_CORRIDOR", 2),
                        ("RIDERS", 8), ("PER_ROUTE", 2), ("REQUESTS", 2)):  # fmt: skip
        monkeypatch.setattr(drill_seed, name, value)


def _seed(tmp_path: Path, fake: Callable[[httpx.Request], httpx.Response], *extra: str) -> int:
    token = tmp_path / "token"
    token.write_text("tok")
    argv = ["seed", "--token-file", str(token), "--state", str(tmp_path / "state.json"),
            "--base-url", "http://test", "--per-minute", "100000", *extra]  # fmt: skip
    return drill_seed.main(argv, transport=httpx.MockTransport(fake))


@pytest.mark.usefixtures("small")
def test_seeding_creates_everything_and_lists_every_id(tmp_path: Path) -> None:
    fake = FakeApi()
    assert _seed(tmp_path, fake, "--launch", "1") == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert {kind: len(ids) for kind, ids in state.items()} == {
        "areas": 4, "corridors": 2, "routes": 4, "riders": 8, "seated": 8, "requests": 2,
    }  # fmt: skip
    creates = [path for method, path, keyed in fake.calls if method == "POST"]
    assert all(keyed for method, _, keyed in fake.calls if method == "POST")
    assert creates.index("/v1/areas") < creates.index("/v1/corridors") < creates.index("/v1/routes")
    statuses = [path for method, path, _ in fake.calls if path.endswith("/status")]
    assert len(statuses) == 4 + 1  # each to forming, then one launched


@pytest.mark.usefixtures("small")
def test_nothing_is_launched_unless_asked(tmp_path: Path) -> None:
    fake = FakeApi()
    _seed(tmp_path, fake)
    assert len([p for _, p, _ in fake.calls if p.endswith("/status")]) == 4


@pytest.mark.usefixtures("small")
def test_seeding_again_refuses_until_removed(tmp_path: Path) -> None:
    _seed(tmp_path, FakeApi())
    with pytest.raises(SystemExit, match="remove it first"):
        _seed(tmp_path, FakeApi())


@pytest.mark.usefixtures("small")
def test_removal_undoes_newest_first_and_can_run_again(tmp_path: Path) -> None:
    fake = FakeApi()
    _seed(tmp_path, fake)
    fake.calls.clear()
    state_file = tmp_path / "state.json"
    argv = ["remove", "--token-file", str(tmp_path / "token"), "--state", str(state_file),
            "--base-url", "http://test", "--per-minute", "100000"]  # fmt: skip
    assert drill_seed.main(argv, transport=httpx.MockTransport(fake)) == 0
    kinds = [path.split("/")[2] for method, path, _ in fake.calls if method == "DELETE"]
    order = [kinds.index(k) for k in ("riders", "route-requests", "routes", "corridors", "areas")]
    assert order == sorted(order)
    assert all(not ids for ids in json.loads(state_file.read_text()).values())
    assert fake.calls[0][1].endswith("/seat/end")  # seats ended before anything is deleted


def test_an_unexpected_answer_stops_naming_the_endpoint_not_the_token(tmp_path: Path) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "FORBIDDEN"})

    with pytest.raises(SystemExit) as stopped:
        _seed(tmp_path, refused)
    assert str(stopped.value) == "POST /v1/areas: 403 FORBIDDEN"


def test_the_next_first_crosses_the_year() -> None:
    from datetime import date

    assert drill_seed.next_first(date(2026, 11, 11)) == date(2026, 12, 1)
    assert drill_seed.next_first(date(2026, 12, 31)) == date(2027, 1, 1)
