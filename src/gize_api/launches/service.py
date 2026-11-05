"""Launch messages (GZ-041): an SMS to each rider a route's launch started.

The launch (GZ-024) answers first; the messages go after it, in the background, on the
app's own sessions (no worker on the free plan: owner decision, GZ-030). Each rider's
message is a GZ-030 `send_once` event keyed `launch:<assignment>:<attempt>`: never sent
twice, and an attempt that failed is final, so a resend is the next attempt. Who gets one:
the seats the launch started (`route_assignments.launched_on`, the latest launch), still
held, whose live rider has a phone. Sent one at a time; one rider's failure never stops
the others, and nothing here can undo the launch. A retry never resends a message sent,
being sent (a `sending` row younger than SENDING_STALE_AFTER), or that may have arrived
(`timeout`: shown `unconfirmed`). Staff check the status after a launch: a restart midway
leaves the rest `pending` until someone retries.
"""

import uuid
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

import structlog
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, record, replay_response
from gize_api.core.security import CurrentStaff
from gize_api.core.telemetry import Telemetry
from gize_api.corridors.models import Corridor
from gize_api.integrations.sms import SmsSender
from gize_api.launches.schemas import LaunchMessage, LaunchMessages, LaunchMessageStatus
from gize_api.riders.models import Rider
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from gize_api.sms.service import Sessions, send_once

# A send takes 10 s at most (core/http.py); a row `sending` for longer was left behind.
SENDING_STALE_AFTER = timedelta(minutes=2)

logger = structlog.get_logger("gize_api.launches")


@dataclass(frozen=True)
class _Attempts:
    count: int
    status: LaunchMessageStatus


_NOT_TRIED = _Attempts(0, LaunchMessageStatus.PENDING)


@dataclass(frozen=True)
class _Recipient:
    seat_id: uuid.UUID
    rider_id: uuid.UUID
    full_name: str
    phone: str
    start_date: date
    tried: _Attempts

    @property
    def attempts(self) -> int:
        return self.tried.count

    @property
    def status(self) -> LaunchMessageStatus:
        return self.tried.status


@dataclass(frozen=True)
class _Launch:
    code: str
    origin: str
    leaves: time
    launch_date: date | None
    recipients: list[_Recipient]


def message(code: str, start: date, origin: str, leaves: time) -> str:
    return (
        f"Gize: your route {code} starts on {start.day} {start:%B %Y}. "
        f"The van leaves {origin} at {leaves:%H:%M}."
    )


async def status(session: AsyncSession, route_id: uuid.UUID) -> LaunchMessages:
    async with session.begin():
        return _messages(await _launch(session, route_id))


async def retry(
    session: AsyncSession,
    caller: CurrentStaff,
    route_id: uuid.UUID,
    idempotent: IdempotentRequest,
) -> tuple[Response, bool]:
    """202 with the messages as they are; True when the caller should start the resend
    (False for a replay of an earlier retry: it started its own)."""
    async with session.begin():
        claimed = await claim(session, scope=f"staff:{caller.id}", request=idempotent)
        if claimed.stored is not None:
            return replay_response(claimed.stored), False
        payload = _messages(await _launch(session, route_id)).model_dump(mode="json")
        await record(session, claimed, status=202, body=payload)
    return JSONResponse(payload, status_code=202), True


def _messages(launch: "_Launch") -> LaunchMessages:
    riders = [
        LaunchMessage(
            rider_id=recipient.rider_id,
            full_name=recipient.full_name,
            start_date=recipient.start_date,
            status=recipient.status,
            attempts=recipient.attempts,
        )
        for recipient in launch.recipients
    ]
    counts = {kind: sum(rider.status is kind for rider in riders) for kind in LaunchMessageStatus}
    return LaunchMessages(
        launch_date=launch.launch_date,
        total=len(riders),
        sent=counts[LaunchMessageStatus.SENT],
        sending=counts[LaunchMessageStatus.SENDING],
        unconfirmed=counts[LaunchMessageStatus.UNCONFIRMED],
        failed=counts[LaunchMessageStatus.FAILED],
        pending=counts[LaunchMessageStatus.PENDING],
        riders=riders,
    )


async def send_due(
    sessions: Sessions,
    sender: SmsSender,
    route_id: uuid.UUID,
    *,
    retry_failed: bool,
    telemetry: Telemetry | None,
) -> None:
    """Send each recipient's message not tried yet; with `retry_failed`, also a new attempt
    for each whose latest failed. Never one sent, being sent, or that may have arrived."""
    due = {LaunchMessageStatus.PENDING}
    if retry_failed:
        due.add(LaunchMessageStatus.FAILED)
    async with sessions() as session, session.begin():
        launch = await _launch(session, route_id)
    for recipient in launch.recipients:
        if recipient.status not in due:
            continue
        try:
            await send_once(
                sessions,
                sender,
                key=f"launch:{recipient.seat_id}:{recipient.attempts + 1}",
                purpose=SmsPurpose.ROUTE_LAUNCH,
                to=recipient.phone,
                text=message(launch.code, recipient.start_date, launch.origin, launch.leaves),
                telemetry=telemetry,
            )
        except Exception:
            logger.warning("launch_message_failed", seat_id=str(recipient.seat_id), exc_info=True)


async def send_in_background(app: Any, route_id: uuid.UUID, *, retry_failed: bool) -> None:  # noqa: ANN401 - the FastAPI app
    """`send_due` after the answer, on the app's sessions and sender: never raises (the
    launch has been answered), logs what stopped it."""
    try:
        await send_due(
            app.state.sessionmaker,
            app.state.sms_sender,
            route_id,
            retry_failed=retry_failed,
            telemetry=app.telemetry,
        )
    except Exception:
        logger.exception("launch_messages_failed", route_id=str(route_id))


async def _launch(session: AsyncSession, route_id: uuid.UUID) -> _Launch:
    found = (
        await session.execute(
            select(Route.code, Route.morning_time, Area.name)
            .join(Corridor, Corridor.id == Route.corridor_id)
            .join(Area, Area.id == Corridor.origin_area_id)
            .where(Route.id == route_id, Route.not_deleted())
        )
    ).one_or_none()
    if found is None:
        raise AppError(ErrorCode.NOT_FOUND)
    code, leaves, origin = found
    launch_date = await session.scalar(
        select(func.max(RouteAssignment.launched_on)).where(RouteAssignment.route_id == route_id)
    )
    if launch_date is None:
        return _Launch(code, origin, leaves, None, [])
    seats = (
        await session.execute(
            select(
                RouteAssignment.id,
                Rider.id,
                Rider.full_name,
                Rider.phone,
                RouteAssignment.start_date,
            )
            .join(Rider, Rider.id == RouteAssignment.rider_id)
            .where(
                RouteAssignment.route_id == route_id,
                RouteAssignment.launched_on == launch_date,
                RouteAssignment.status != AssignmentStatus.ENDED,
                Rider.phone.is_not(None),
                Rider.not_deleted(),
            )
            .order_by(Rider.full_name, Rider.id)
        )
    ).all()
    tried = await _attempts(session, [seat_id for seat_id, *_ in seats])
    return _Launch(
        code,
        origin,
        leaves,
        launch_date,
        [
            _Recipient(seat_id, rider_id, name, str(phone), start, tried.get(seat_id, _NOT_TRIED))
            for seat_id, rider_id, name, phone, start in seats
        ],
    )


async def _attempts(session: AsyncSession, seat_ids: list[uuid.UUID]) -> dict[uuid.UUID, _Attempts]:
    """Per seat: how many launch SMS were tried, and where the latest stands. Only launch
    rows are read (`ix_sms_messages_route_launch`, partial)."""
    if not seat_ids:
        return {}
    seat = func.split_part(SmsMessage.dedup_key, ":", 2)
    attempt = cast(func.split_part(SmsMessage.dedup_key, ":", 3), Integer)
    fresh = SmsMessage.updated_at > func.now() - SENDING_STALE_AFTER
    rows = await session.execute(
        select(seat, attempt, SmsMessage.status, SmsMessage.error, fresh)
        .where(
            SmsMessage.purpose == SmsPurpose.ROUTE_LAUNCH,
            seat.in_([str(seat_id) for seat_id in seat_ids]),
        )
        .order_by(seat, attempt)
    )
    found: dict[uuid.UUID, _Attempts] = {}
    for seat_text, number, sms_status, error, recent in rows:  # the latest wins
        found[uuid.UUID(seat_text)] = _Attempts(number, _where(sms_status, error, recent))
    return found


def _where(status: SmsStatus, error: str | None, recent: bool) -> LaunchMessageStatus:
    if status is SmsStatus.SENT:
        return LaunchMessageStatus.SENT
    if status is SmsStatus.SENDING:
        # Still under way, or the process died in it (final for its key: GZ-030).
        return LaunchMessageStatus.SENDING if recent else LaunchMessageStatus.FAILED
    if error == "timeout":
        return LaunchMessageStatus.UNCONFIRMED
    return LaunchMessageStatus.FAILED
