"""GZ-041: riders get an SMS when their route launches ("today" is 15 November 2026).

When staff launch a forming route (GZ-024), the answer comes at once; then, in the
background, every rider the launch started gets one SMS: "Gize: your route <code> starts
on <their first day>. The van leaves <A> at <time>." Sent through GZ-030's `send_once`,
one key per rider and attempt, so nothing is sent twice. An SMS failure never undoes the
launch: staff see each rider's message in `GET /v1/routes/{id}/launch-messages` and resend
the failed ones with `POST .../launch-messages/retry`. Company routes send nothing.
"""

import re
import uuid
from datetime import date

import httpx
import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.integrations.sms import FakeSmsSender, SmsSendError
from gize_api.main import GizeAPI
from gize_api.routes.models import AssignmentStatus, RouteAssignment, RouteStatus, RouteType
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from tests.factories import CompanyFactory, RiderFactory
from tests.registrations.support import Trip, add_route, add_trip
from tests.staff.support import add_staff, signed_in

LAUNCH = {"status": "active", "launch_date": "2026-12-01"}


@pytest.fixture
async def trip(db_session: AsyncSession) -> Trip:
    return await add_trip(db_session)


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def _reserved(
    session: AsyncSession, route: uuid.UUID, trip: Trip, phone: str, start: date
) -> uuid.UUID:
    async with session.begin():
        rider = await RiderFactory().create(
            session, phone=phone, home_area_id=trip.bole, destination_area_id=trip.mexico
        )
        seat = RouteAssignment(
            route_id=route, rider_id=rider.id, start_date=start, status=AssignmentStatus.RESERVED
        )
        session.add(seat)
        await session.flush()
        return rider.id


async def _forming(session: AsyncSession, trip: Trip, *riders: tuple[str, date]) -> uuid.UUID:
    route = await add_route(session, trip, code="BOLE-MEX-01", launch_minimum=1)
    for phone, start in riders:
        await _reserved(session, route, trip, phone, start)
    return route


async def _launch(api: httpx.AsyncClient, staff: uuid.UUID, route: uuid.UUID) -> httpx.Response:
    return await api.put(f"/v1/routes/{route}/status", json=LAUNCH, headers=signed_in(staff))


def _messages(route: uuid.UUID) -> str:
    return f"/v1/routes/{route}/launch-messages"


async def _rows(session: AsyncSession) -> list[SmsMessage]:
    async with session.begin():
        statement = select(SmsMessage).order_by(SmsMessage.created_at, SmsMessage.dedup_key)
        return list((await session.scalars(statement)).all())


class Failing:
    """A gateway that refuses everything (or raises something unexpected)."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or SmsSendError("unavailable")
        self.tried: list[str] = []

    async def send(self, to: str, text: str) -> str:
        self.tried.append(to)
        raise self.error

    async def aclose(self) -> None:
        return None


# Sending


async def test_each_rider_the_launch_starts_gets_one_sms(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    route = await _forming(
        db_session,
        trip,
        ("+251911000001", date(2026, 12, 1)),
        ("+251911000002", date(2027, 1, 1)),  # reserved from January: starts then
    )
    response = await _launch(api, member, route)
    assert response.status_code == 200, response.json()
    assert sorted(sms.sent) == [
        ("+251911000001", "Gize: your route BOLE-MEX-01 starts on 1 December 2026. "
                          "The van leaves Bole at 06:30."),
        ("+251911000002", "Gize: your route BOLE-MEX-01 starts on 1 January 2027. "
                          "The van leaves Bole at 06:30."),
    ]  # fmt: skip
    rows = await _rows(db_session)
    assert {(row.purpose, row.status) for row in rows} == {
        (SmsPurpose.ROUTE_LAUNCH, SmsStatus.SENT)
    }
    assert all(re.fullmatch(r"launch:[0-9a-f-]{36}:1", row.dedup_key) for row in rows)


async def test_a_repeated_launch_sends_nothing_more(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    route = await _forming(db_session, trip, ("+251911000001", date(2026, 12, 1)))
    await _launch(api, member, route)
    again = await _launch(api, member, route)  # active already: a no-op
    assert again.status_code == 200
    assert len(sms.sent) == 1


async def test_riders_placed_after_the_launch_get_no_launch_sms(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    route = await _forming(db_session, trip, ("+251911000001", date(2026, 12, 1)))
    await _launch(api, member, route)
    async with db_session.begin():
        later = await RiderFactory().create(
            db_session, home_area_id=trip.bole, destination_area_id=trip.mexico
        )
        later_id = later.id
    placed = await api.put(
        f"/v1/riders/{later_id}/seat",
        json={"route_id": str(route)},
        headers={**signed_in(member), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert placed.status_code == 200, placed.json()
    status = (await api.get(_messages(route), headers=signed_in(member))).json()
    assert status["total"] == 1
    retried = await api.post(f"{_messages(route)}/retry", headers=signed_in(member))
    assert retried.status_code == 202
    assert len(sms.sent) == 1


async def test_a_company_route_turned_on_sends_nothing(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    """The company tells its own staff (owner decision)."""
    async with db_session.begin():
        company = (await CompanyFactory().create(db_session)).id
    route = await add_route(
        db_session, trip, route_type=RouteType.CORPORATE, company_id=company,
        status=RouteStatus.DRAFT, launch_minimum=None, price_round_trip=None,
        price_one_way=None,
    )  # fmt: skip
    response = await api.put(
        f"/v1/routes/{route}/status", json={"status": "active"}, headers=signed_in(member)
    )
    assert response.status_code == 200, response.json()
    assert sms.sent == []


# Failures, status, retry


async def test_an_sms_failure_never_undoes_the_launch(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    api_app.state.sms_sender = Failing()
    route = await _forming(db_session, trip, ("+251911000001", date(2026, 12, 1)))
    response = await _launch(api, member, route)
    assert (response.status_code, response.json()["status"]) == (200, "active")
    (row,) = await _rows(db_session)
    assert (row.status, row.error) == (SmsStatus.FAILED, "unavailable")


async def test_staff_see_each_riders_message_and_resend_the_failed_ones(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    route = await _forming(
        db_session,
        trip,
        ("+251911000001", date(2026, 12, 1)),
        ("+251911000002", date(2026, 12, 1)),
    )
    failing = Failing()
    api_app.state.sms_sender = failing
    await _launch(api, member, route)
    api_app.state.sms_sender = sms
    status = await api.get(_messages(route), headers=signed_in(member))
    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    body = status.json()
    assert (body["launch_date"], body["total"], body["sent"], body["failed"], body["pending"]) == (
        "2026-12-01", 2, 0, 2, 0,
    )  # fmt: skip
    assert {rider["status"] for rider in body["riders"]} == {"failed"}
    assert {rider["attempts"] for rider in body["riders"]} == {1}
    assert set(body["riders"][0]) == {"rider_id", "full_name", "start_date", "status", "attempts"}

    retried = await api.post(f"{_messages(route)}/retry", headers=signed_in(member))
    assert retried.status_code == 202
    assert len(sms.sent) == 2
    after = (await api.get(_messages(route), headers=signed_in(member))).json()
    assert (after["sent"], after["failed"]) == (2, 0)
    assert {rider["attempts"] for rider in after["riders"]} == {2}
    again = await api.post(f"{_messages(route)}/retry", headers=signed_in(member))
    assert again.status_code == 202
    assert len(sms.sent) == 2  # the sent ones are never sent again
    keys = sorted(row.dedup_key.rsplit(":", 1)[1] for row in await _rows(db_session))
    assert keys == ["1", "1", "2", "2"]


async def test_one_riders_unexpected_error_doesnt_stop_the_others(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    failing = Failing(RuntimeError("bug in the gateway client"))
    api_app.state.sms_sender = failing
    route = await _forming(
        db_session,
        trip,
        ("+251911000001", date(2026, 12, 1)),
        ("+251911000002", date(2026, 12, 1)),
    )
    with structlog.testing.capture_logs() as logs:
        response = await _launch(api, member, route)
    assert response.status_code == 200
    assert len(failing.tried) == 2
    assert [log["event"] for log in logs].count("launch_message_failed") == 2
    status = (await api.get(_messages(route), headers=signed_in(member))).json()
    assert {rider["status"] for rider in status["riders"]} == {"failed"}
    assert "+251911000001" not in repr(logs)


async def test_a_message_never_attempted_is_pending_and_retried(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    db_session: AsyncSession,
    sms: FakeSmsSender,
    trip: Trip,
    member: uuid.UUID,
) -> None:
    """The process stopped before sending (a restart): retry sends it."""
    route = await _forming(db_session, trip, ("+251911000001", date(2026, 12, 1)))
    del api_app.state.sms_sender  # the background can't send at all
    with structlog.testing.capture_logs() as logs:
        await _launch(api, member, route)
    assert any(log["event"] == "launch_messages_failed" for log in logs)
    api_app.state.sms_sender = sms
    status = (await api.get(_messages(route), headers=signed_in(member))).json()
    assert (status["pending"], status["riders"][0]["status"], status["riders"][0]["attempts"]) == (
        1, "pending", 0,
    )  # fmt: skip
    await api.post(f"{_messages(route)}/retry", headers=signed_in(member))
    assert len(sms.sent) == 1


async def test_a_route_never_launched_has_no_launch_messages(
    api: httpx.AsyncClient, db_session: AsyncSession, trip: Trip, member: uuid.UUID
) -> None:
    route = await _forming(db_session, trip, ("+251911000001", date(2026, 12, 1)))
    status = (await api.get(_messages(route), headers=signed_in(member))).json()
    assert status == {
        "launch_date": None, "total": 0, "sent": 0, "failed": 0, "pending": 0, "riders": [],
    }  # fmt: skip


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_unknown_route_is_404(
    api: httpx.AsyncClient, member: uuid.UUID, method: str
) -> None:
    path = _messages(uuid.uuid4()) + ("/retry" if method == "POST" else "")
    response = await api.request(method, path, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")
