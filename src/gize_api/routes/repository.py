"""Route queries: live (not deleted) routes, and the seats their riders hold.

A seat is held on every day from an assignment's start date to its end date unless it has
ended. Counts are per day; a month's count is its busiest day.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import (
    ColumnElement,
    Date,
    Select,
    and_,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from gize_api.core.db import LIKE_ESCAPE, escape_like
from gize_api.corridors.models import Corridor
from gize_api.offers.models import OfferStatus, SeatOffer
from gize_api.riders.models import Rider
from gize_api.route_requests.models import OPEN_STATUSES, RequestKind, RouteRequest
from gize_api.routes.models import (
    AssignmentStatus,
    Route,
    RouteArea,
    RouteAssignment,
    RouteStatus,
    RouteType,
)

# The corridor (with both areas) and company in the same query; inner joins where required.
# Public routes (GZ-036) have no company: they leave it out.
_WITH_PUBLIC_PLACES = (
    joinedload(Route.corridor, innerjoin=True).joinedload(Corridor.origin_area, innerjoin=True),
    joinedload(Route.corridor, innerjoin=True).joinedload(
        Corridor.destination_area, innerjoin=True
    ),
    selectinload(Route.route_areas).joinedload(RouteArea.area, innerjoin=True),
)
_WITH_PLACES = (*_WITH_PUBLIC_PLACES, joinedload(Route.company), joinedload(Route.driver))
_HOLDS_A_SEAT = RouteAssignment.status != AssignmentStatus.ENDED
_RESERVED = RouteAssignment.status == AssignmentStatus.RESERVED


def _reserved_for(launch_day: date) -> tuple[ColumnElement[bool], ...]:
    """Riders a launch on `launch_day` counts: reserved from that day or earlier, and not
    gone before it. Launch progress and the launch itself both use this rule."""
    return (
        _RESERVED,
        RouteAssignment.start_date <= launch_day,
        or_(RouteAssignment.end_date.is_(None), RouteAssignment.end_date >= launch_day),
    )


def holds_a_seat_from(today: date) -> tuple[ColumnElement[bool], ...]:
    """A seat held today or later: not ended, and not gone before today."""
    return (
        _HOLDS_A_SEAT,
        or_(RouteAssignment.end_date.is_(None), RouteAssignment.end_date >= today),
    )


@dataclass(frozen=True)
class SeatCounts:
    this_month: int = 0
    next_month: int = 0
    reserved: int = 0
    held: bool = False  # someone holds a seat today or later (`has_riders`)


async def get_live_route(
    session: AsyncSession, route_id: uuid.UUID, *, for_update: bool = False
) -> Route | None:
    """The route with its corridor, areas and company. `for_update` locks the route row
    alone first, then loads it (a locking query with the joins would, after a wait, judge
    the route against places read before the wait; see corridors.repository)."""
    live = (Route.id == route_id, Route.not_deleted())
    # FOR NO KEY UPDATE: serialises changes to the route (and M3's rider inserts, which
    # take it first) without blocking inserts that only refer to it (its served areas).
    locked = select(Route.id).where(*live).with_for_update(key_share=True)
    if for_update and await session.scalar(locked) is None:
        return None
    return await session.scalar(select(Route).options(*_WITH_PLACES).where(*live))


async def refer_to_live_route(session: AsyncSession, route_id: uuid.UUID) -> Route | None:
    """The live route, share-locked (FOR SHARE) for something new that refers to it or copies
    it (a route request, GZ-027; a van, GZ-028): the route's changes and delete, which take
    its row lock, wait for it, or it waits for them and reads the result (or finds the
    route gone). Loaded without its places."""
    live = (Route.id == route_id, Route.not_deleted())
    if await session.scalar(select(Route.id).where(*live).with_for_update(read=True)) is None:
        return None
    return await session.scalar(select(Route).where(*live))


async def code_taken(session: AsyncSession, code: str, *, except_id: uuid.UUID | None) -> bool:
    statement = exists().where(Route.code == code, Route.not_deleted())
    if except_id is not None:
        statement = statement.where(Route.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_routes(
    *,
    q: str | None,
    route_type: RouteType | None,
    status: RouteStatus | None,
    corridor_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
) -> Select[Route]:
    """The filtered routes with their places, unordered: `paginate` orders and bounds."""
    statement = select(Route).options(*_WITH_PLACES).where(Route.not_deleted())
    if q:
        statement = statement.where(
            Route.code.startswith(func.upper(literal(escape_like(q))), escape=LIKE_ESCAPE)
        )
    for column, value in (
        (Route.route_type, route_type),
        (Route.status, status),
        (Route.corridor_id, corridor_id),
        (Route.company_id, company_id),
    ):
        if value is not None:
            statement = statement.where(column == value)
    return statement


# Routes an individual rider can join or ask for (GZ-036, GZ-032): reserve on a forming
# one, request a seat on a running one. Priced: forming needs prices, so all of these have.
_OPEN_TO_RIDERS = (RouteStatus.FORMING, RouteStatus.ACTIVE, RouteStatus.PAUSED)


def serves_trip(from_area_id: uuid.UUID, to_area_id: uuid.UUID) -> ColumnElement[bool]:
    """The route takes a rider from `from_area_id` to `to_area_id`: boarding at A or an
    area it serves, getting off at B (the morning run; the evening run is the way back)."""
    ends_at_b = select(Corridor.id).where(Corridor.destination_area_id == to_area_id)
    starts_at_a = select(Corridor.id).where(Corridor.origin_area_id == from_area_id)
    boards_on_the_way = exists().where(
        RouteArea.route_id == Route.id, RouteArea.area_id == from_area_id
    )
    return and_(
        Route.corridor_id.in_(ends_at_b),
        or_(Route.corridor_id.in_(starts_at_a), boards_on_the_way),
    )


async def serves(
    session: AsyncSession, route_id: uuid.UUID, from_area_id: uuid.UUID, to_area_id: uuid.UUID
) -> bool:
    """The route takes a rider on this trip (`serves_trip`)."""
    statement = select(Route.id).where(Route.id == route_id, serves_trip(from_area_id, to_area_id))
    return await session.scalar(statement) is not None


def public_routes(*, from_area_id: uuid.UUID, to_area_id: uuid.UUID) -> Select[Route]:
    """Live public routes riders can join or ask for, serving the trip, with their places,
    unordered: `paginate` orders and bounds."""
    return (
        select(Route)
        .options(*_WITH_PUBLIC_PLACES)
        .where(*_public_open(), serves_trip(from_area_id, to_area_id))
    )


def _public_open() -> tuple[ColumnElement[bool], ...]:
    return (
        Route.not_deleted(),
        Route.route_type == RouteType.PUBLIC,
        Route.status.in_(_OPEN_TO_RIDERS),
        Route.price_round_trip.is_not(None),
        Route.price_one_way.is_not(None),
    )


async def fitting_public_routes(
    session: AsyncSession, from_area_id: uuid.UUID, to_area_id: uuid.UUID, *, limit: int
) -> list[Route]:
    """Up to `limit` of `public_routes` for the trip, without their places."""
    statement = (
        select(Route)
        .where(*_public_open(), serves_trip(from_area_id, to_area_id))
        .order_by(Route.created_at, Route.id)
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


async def seat_counts(
    session: AsyncSession, route_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, SeatCounts]:
    """Seats taken on the busiest day of this month (from today) and of next month, riders
    reserved for the next launch day (`next_launch_day`: launch progress), and whether
    anyone holds a seat today or later, for every route in two queries."""
    if not route_ids:
        return {}
    next_month = first_of_next_month(today)
    last_day = first_of_next_month(next_month) - timedelta(days=1)
    launch_day = next_launch_day(today)
    peaks = {
        route_id: (this_month, next)
        for route_id, this_month, next in await session.execute(
            _peaks(route_ids, today, last_day, split=next_month)
        )
    }
    # Riders from today on; those reserved for the launch day are among them (it's today
    # or later). A route nobody holds has no row.
    reserved = {
        route_id: count
        for route_id, count in await session.execute(
            select(
                RouteAssignment.route_id,
                func.count().filter(and_(*_reserved_for(launch_day))),
            )
            .where(RouteAssignment.route_id.in_(route_ids), *holds_a_seat_from(today))
            .group_by(RouteAssignment.route_id)
        )
    }
    return {
        route_id: SeatCounts(
            *peaks.get(route_id, (0, 0)),
            reserved=reserved.get(route_id, 0),
            held=route_id in reserved,
        )
        for route_id in route_ids
    }


async def boarded_in(
    session: AsyncSession, route_id: uuid.UUID, area_ids: set[uuid.UUID], today: date
) -> bool:
    """A rider holding a seat today or later boards in one of these areas (their home)."""
    statement = exists().where(
        RouteAssignment.route_id == route_id,
        *holds_a_seat_from(today),
        Rider.id == RouteAssignment.rider_id,
        Rider.home_area_id.in_(area_ids),
    )
    return bool(await session.scalar(select(statement)))


async def saturday_held(session: AsyncSession, route_id: uuid.UUID, today: date) -> bool:
    """A rider holding a seat today or later has the Saturday add-on."""
    statement = exists().where(
        RouteAssignment.route_id == route_id,
        *holds_a_seat_from(today),
        Rider.id == RouteAssignment.rider_id,
        Rider.saturday_addon.is_(True),
    )
    return bool(await session.scalar(select(statement)))


async def reserved_by_month(
    session: AsyncSession, route_id: uuid.UUID, launch_days: list[date]
) -> list[int]:
    """Riders a launch on each of `launch_days` would start (`_reserved_for`), in one
    query (one count per day; callers pass at most 13)."""
    row = (
        await session.execute(
            select(*(func.count().filter(and_(*_reserved_for(day))) for day in launch_days))
            .select_from(RouteAssignment)
            .where(RouteAssignment.route_id == route_id, _RESERVED)
        )
    ).one()
    return [int(count) for count in row]


async def peak_taken_from(session: AsyncSession, route_id: uuid.UUID, today: date) -> int:
    """Seats taken on the busiest day from today on. After the last start date no one
    joins, so that is the last day worth counting."""
    last_start = await session.scalar(
        select(func.max(RouteAssignment.start_date)).where(
            RouteAssignment.route_id == route_id, _HOLDS_A_SEAT
        )
    )
    last_day = max(today, last_start or today)
    row = (await session.execute(_peaks([route_id], today, last_day, split=None))).first()
    return 0 if row is None else int(row[1])


def _holding_offers() -> tuple[ColumnElement[bool], ...]:
    """Open offers in time whose request is still live, open and waiting for the offer's
    route (asked for it, or matched to it): they hold a seat."""
    return (
        SeatOffer.status == OfferStatus.OPEN,
        SeatOffer.expires_at > func.now(),
        exists().where(
            RouteRequest.id == SeatOffer.request_id,
            RouteRequest.not_deleted(),
            RouteRequest.status.in_(OPEN_STATUSES),
            or_(
                and_(
                    RouteRequest.kind == RequestKind.ROUTE,
                    RouteRequest.route_id == SeatOffer.route_id,
                ),
                RouteRequest.matched_route_id == SeatOffer.route_id,
            ),
        ),
    )


async def held_by_offers(
    session: AsyncSession, route_id: uuid.UUID, *, excluding: uuid.UUID | None = None
) -> int:
    """Seats open offers hold on the route (GZ-040): open, in time, their request still
    open. Every way of taking a seat counts them; an offer being accepted doesn't count
    itself."""
    statement = select(func.count()).where(SeatOffer.route_id == route_id, *_holding_offers())
    if excluding is not None:
        statement = statement.where(SeatOffer.id != excluding)
    return int(await session.scalar(statement.select_from(SeatOffer)) or 0)


async def held_by_offers_on(
    session: AsyncSession, route_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """`held_by_offers` for several routes at once (one query); a route with none has no key."""
    if not route_ids:
        return {}
    rows = await session.execute(
        select(SeatOffer.route_id, func.count())
        .where(SeatOffer.route_id.in_(route_ids), *_holding_offers())
        .group_by(SeatOffer.route_id)
    )
    return {route_id: int(count) for route_id, count in rows}


async def taken_with_offers(
    session: AsyncSession, route_id: uuid.UUID, first_day: date, *, excluding: uuid.UUID | None
) -> int:
    """Seats taken on the busiest day from `first_day`, plus those open offers hold."""
    taken = await peak_taken_from(session, route_id, first_day)
    return taken + await held_by_offers(session, route_id, excluding=excluding)


async def latest_launch_day(session: AsyncSession, route_id: uuid.UUID) -> date | None:
    """The day the route's launch started its riders (GZ-041's `launched_on`)."""
    return await session.scalar(
        select(func.max(RouteAssignment.launched_on)).where(RouteAssignment.route_id == route_id)
    )


async def has_riders(session: AsyncSession, route_id: uuid.UUID, today: date) -> bool:
    """Someone holds a seat today or later."""
    statement = exists().where(RouteAssignment.route_id == route_id, *holds_a_seat_from(today))
    return bool(await session.scalar(select(statement)))


async def ready_to_launch(session: AsyncSession, today: date) -> int:
    """Forming routes whose riders reserved for the next launch day reach their launch
    minimum (GZ-064): launch progress's rule (`_reserved_for`), one query."""
    launch_day = next_launch_day(today)
    reserved = (
        select(func.count())
        .where(RouteAssignment.route_id == Route.id, *_reserved_for(launch_day))
        .correlate(Route)
        .scalar_subquery()
    )
    statement = select(func.count()).where(
        Route.not_deleted(),
        Route.status == RouteStatus.FORMING,
        Route.launch_minimum.is_not(None),
        reserved >= Route.launch_minimum,
    )
    return int(await session.scalar(statement) or 0)


async def reserved_for(session: AsyncSession, route_id: uuid.UUID, launch_day: date) -> int:
    """Riders a launch on `launch_day` would start (`_reserved_for`)."""
    statement = select(func.count()).where(
        RouteAssignment.route_id == route_id, *_reserved_for(launch_day)
    )
    return int(await session.scalar(statement.select_from(RouteAssignment)) or 0)


async def start_reserved(
    session: AsyncSession, route_id: uuid.UUID, launch_day: date
) -> tuple[int, int]:
    """Launch: every reservation becomes active, from `launch_day` if it started earlier
    and from its own start date if later; one that ended before `launch_day` lapses (ends:
    it never rode). Returns (riders started, reservations lapsed). Run under the route's
    lock, which adding a reservation takes too (M3)."""
    reserved = (RouteAssignment.route_id == route_id, _RESERVED)
    lapsed = await session.scalars(
        update(RouteAssignment)
        .where(*reserved, RouteAssignment.end_date < launch_day)
        .values(status=AssignmentStatus.ENDED)
        .returning(RouteAssignment.id)
    )
    lapsed_count = len(lapsed.all())
    started = await session.scalars(
        update(RouteAssignment)
        .where(*reserved)
        .values(
            status=AssignmentStatus.ACTIVE,
            start_date=func.greatest(RouteAssignment.start_date, literal(launch_day, Date)),
            launched_on=launch_day,  # GZ-041: who gets the launch SMS
        )
        .returning(RouteAssignment.id)
    )
    return len(started.all()), lapsed_count


async def serves_area(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live route serves this area (index: ix_route_areas_area_id)."""
    statement = exists().where(
        RouteArea.area_id == area_id, RouteArea.route_id == Route.id, Route.not_deleted()
    )
    return bool(await session.scalar(select(statement)))


async def uses_corridor(session: AsyncSession, corridor_id: uuid.UUID) -> bool:
    """A live route runs on this corridor (index: ix_routes_corridor_id)."""
    statement = exists().where(Route.corridor_id == corridor_id, Route.not_deleted())
    return bool(await session.scalar(select(statement)))


async def company_runs_a_route(session: AsyncSession, company_id: uuid.UUID) -> bool:
    """One of the company's live routes is active or paused (index: ix_routes_company_id)."""
    statement = exists().where(
        Route.company_id == company_id,
        Route.not_deleted(),
        Route.status.in_((RouteStatus.ACTIVE, RouteStatus.PAUSED)),
    )
    return bool(await session.scalar(select(statement)))


async def company_has_a_route(session: AsyncSession, company_id: uuid.UUID) -> bool:
    statement = exists().where(Route.company_id == company_id, Route.not_deleted())
    return bool(await session.scalar(select(statement)))


def _peaks(
    route_ids: list[uuid.UUID], first_day: date, last_day: date, *, split: date | None
) -> Select[uuid.UUID, int, int]:
    """Per route, the most seats held on any day from `first_day` to `last_day`: before
    `split` and from it (with no split, the whole span, twice). Days nobody holds a seat
    don't appear, so a route with none has no row (0).

    The seats overlapping the span are picked once (a materialised CTE on the route and
    start-date index), then counted per day: the cost follows the span, not the routes'
    whole history (assignments are never deleted).
    """
    overlapping = (
        select(RouteAssignment.route_id, RouteAssignment.start_date, RouteAssignment.end_date)
        .where(
            RouteAssignment.route_id.in_(route_ids),
            _HOLDS_A_SEAT,
            RouteAssignment.start_date <= last_day,
            or_(RouteAssignment.end_date.is_(None), RouteAssignment.end_date >= first_day),
        )
        .cte("overlapping")
        .prefix_with("MATERIALIZED")
    )
    offsets = func.generate_series(0, (last_day - first_day).days).column_valued("offset")
    day = cast(literal(first_day, Date) + offsets, Date)  # date + integer: a date, no zones
    daily = (
        select(overlapping.c.route_id, day.label("day"), func.count().label("taken"))
        .where(
            overlapping.c.start_date <= day,
            or_(overlapping.c.end_date.is_(None), overlapping.c.end_date >= day),
        )
        .group_by(overlapping.c.route_id, day)
        .subquery()
    )
    before = daily.c.day < split if split is not None else literal(True)
    after = daily.c.day >= split if split is not None else literal(True)
    return select(
        daily.c.route_id,
        func.coalesce(func.max(daily.c.taken).filter(before), 0),
        func.coalesce(func.max(daily.c.taken).filter(after), 0),
    ).group_by(daily.c.route_id)


def next_launch_day(today: date) -> date:
    """The earliest day a forming route can launch: today if it's the 1st, else the 1st of
    next month (individual riders start on the 1st). Launch progress counts the riders a
    launch that day would start (`_reserved_for`)."""
    return today if today.day == 1 else first_of_next_month(today)


def first_of_next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)
