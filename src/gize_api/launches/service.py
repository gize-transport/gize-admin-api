"""Launch messages (GZ-041): an SMS to each rider a route's launch started.

The launch (GZ-024) answers first; the messages go after it, in the background, on the
app's own sessions (no worker on the free plan: owner decision, GZ-030). Each rider's
message is a GZ-030 `send_once` event keyed `launch:<assignment>:<attempt>`: never sent
twice, and an attempt that failed is final, so a resend is the next attempt. Who gets one:
the seats the launch started (`route_assignments.launched_on`, the latest launch), still
held, whose rider has a phone. Sent one at a time; one rider's failure never stops the
others, and nothing here can undo the launch.
"""

import uuid
from dataclasses import dataclass
from datetime import date, time
from typing import Any

import structlog
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.areas.models import Area
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.telemetry import Telemetry
from gize_api.corridors.models import Corridor
from gize_api.integrations.sms import SmsSender
from gize_api.launches.schemas import LaunchMessage, LaunchMessages, LaunchMessageStatus
from gize_api.riders.models import Rider
from gize_api.routes.models import AssignmentStatus, Route, RouteAssignment
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from gize_api.sms.service import Sessions, send_once

logger = structlog.get_logger("gize_api.launches")


@dataclass(frozen=True)
class _Recipient:
    seat_id: uuid.UUID
    rider_id: uuid.UUID
    full_name: str
    phone: str
    start_date: date
    attempts: int
    latest: SmsStatus | None


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
        launch = await _launch(session, route_id)
    riders = [
        LaunchMessage(
            rider_id=recipient.rider_id,
            full_name=recipient.full_name,
            start_date=recipient.start_date,
            status=_status(recipient),
            attempts=recipient.attempts,
        )
        for recipient in launch.recipients
    ]
    counts = {kind: sum(rider.status is kind for rider in riders) for kind in LaunchMessageStatus}
    return LaunchMessages(
        launch_date=launch.launch_date,
        total=len(riders),
        sent=counts[LaunchMessageStatus.SENT],
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
    """Send each recipient's message not attempted yet; with `retry_failed`, also a new
    attempt for each whose latest failed. Never one that was sent."""
    async with sessions() as session, session.begin():
        launch = await _launch(session, route_id)
    for recipient in launch.recipients:
        if recipient.latest is SmsStatus.SENT or (recipient.attempts and not retry_failed):
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
            )
            .order_by(Rider.full_name, Rider.id)
        )
    ).all()
    attempts = await _attempts(session, [seat_id for seat_id, *_ in seats])
    return _Launch(
        code,
        origin,
        leaves,
        launch_date,
        [
            _Recipient(
                seat_id, rider_id, name, str(phone), start, *attempts.get(seat_id, (0, None))
            )
            for seat_id, rider_id, name, phone, start in seats
        ],
    )


async def _attempts(
    session: AsyncSession, seat_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, SmsStatus]]:
    """Per seat: how many launch SMS were tried, and the latest one's status."""
    if not seat_ids:
        return {}
    seat = func.split_part(SmsMessage.dedup_key, ":", 2)
    attempt = cast(func.split_part(SmsMessage.dedup_key, ":", 3), Integer)
    rows = await session.execute(
        select(seat, attempt, SmsMessage.status)
        .where(
            SmsMessage.purpose == SmsPurpose.ROUTE_LAUNCH,
            seat.in_([str(seat_id) for seat_id in seat_ids]),
        )
        .order_by(seat, attempt)
    )
    found: dict[uuid.UUID, tuple[int, SmsStatus]] = {}
    for seat_text, number, sms_status in rows:
        found[uuid.UUID(seat_text)] = (number, sms_status)  # the latest wins
    return found


def _status(recipient: _Recipient) -> LaunchMessageStatus:
    if recipient.latest is None:
        return LaunchMessageStatus.PENDING
    if recipient.latest is SmsStatus.SENT:
        return LaunchMessageStatus.SENT
    return LaunchMessageStatus.FAILED  # failed, or left `sending` (final for its key)
