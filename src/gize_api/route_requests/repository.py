"""Route request queries: live (not deleted) requests only."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    SQLColumnExpression,
    String,
    Uuid,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from gize_api.areas.models import Area
from gize_api.offers.models import (
    NEXT,
    NOT_OFFERABLE,
    PASSED_OVER,
    NotOfferable,
    OfferStatus,
    QueueKey,
    SeatOffer,
)
from gize_api.route_requests.models import (
    OPEN_STATUSES,
    RequestKind,
    RequestStatus,
    RiderType,
    RouteRequest,
)
from gize_api.routes.models import Route

# The areas and routes it names, in the same query (outer joins: each may be absent).
_WITH_PLACES = (
    joinedload(RouteRequest.from_area),
    joinedload(RouteRequest.to_area),
    joinedload(RouteRequest.route),
    joinedload(RouteRequest.matched_route),
)
_OPEN = RouteRequest.status.in_(OPEN_STATUSES)


@dataclass(frozen=True)
class Asked:
    """What one request asks for, as the duplicate rule compares it."""

    contact_phone: str
    kind: RequestKind
    route_id: uuid.UUID | None
    from_area_id: uuid.UUID | None
    from_area_name: str | None
    to_area_id: uuid.UUID | None
    to_area_name: str | None
    rider_name: str


@dataclass(frozen=True)
class PairDemand:
    from_area_id: uuid.UUID | None
    from_area_name: str | None
    to_area_id: uuid.UUID | None
    to_area_name: str | None
    open: int


async def get_live_request(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> RouteRequest | None:
    """The request with what it names. `for_update` locks its row alone first, then loads
    it (see routes.repository.get_live_route)."""
    live = (RouteRequest.id == request_id, RouteRequest.not_deleted())
    locked = select(RouteRequest.id).where(*live).with_for_update()
    if for_update and await session.scalar(locked) is None:
        return None
    return await session.scalar(select(RouteRequest).options(*_WITH_PLACES).where(*live))


def _area_key(
    area_id: SQLColumnExpression[Any], name: SQLColumnExpression[Any]
) -> SQLColumnExpression[Any]:
    return func.coalesce(cast(area_id, String), func.lower(name), "")


def _keys(
    phone: SQLColumnExpression[Any],
    kind: SQLColumnExpression[Any],
    route_id: SQLColumnExpression[Any],
    from_area: SQLColumnExpression[Any],
    to_area: SQLColumnExpression[Any],
    rider_name: SQLColumnExpression[Any],
) -> tuple[SQLColumnExpression[Any], ...]:
    """The columns of `uq_route_requests_open`, in its order."""
    return (
        phone,
        kind,
        func.coalesce(cast(route_id, String), ""),
        from_area,
        to_area,
        func.lower(rider_name),
    )


async def own_requests_for(
    session: AsyncSession,
    route_id: uuid.UUID,
    *,
    rider_id: uuid.UUID,
    phone: str | None,
    name: str,
) -> list[RouteRequest]:
    """The rider's open requests waiting for this route (GZ-035): theirs by `rider_id`, or
    recorded by staff for them before they were a rider (their phone and name, ignoring
    case: a relative asking with the same phone is someone else). Locked (`FOR UPDATE`):
    placing them fulfils these. Oldest first."""
    recorded_for_them = and_(
        RouteRequest.rider_id.is_(None),
        RouteRequest.contact_phone == phone,
        func.lower(RouteRequest.rider_name) == func.lower(literal(name, String)),
    )
    statement = (
        select(RouteRequest)
        .where(*waiting_for(route_id), or_(RouteRequest.rider_id == rider_id, recorded_for_them))
        .order_by(RouteRequest.created_at, RouteRequest.id)
        .with_for_update()
    )
    return list((await session.scalars(statement)).all())


async def others_waiting(
    session: AsyncSession,
    route_id: uuid.UUID,
    *,
    exclude: list[uuid.UUID],
    before: datetime | None,
) -> bool:
    """Someone else's open request waits for this route (asked for it, or matched to it):
    any, or only those made `before` (the rider's own oldest request)."""
    statement = exists().where(*waiting_for(route_id), RouteRequest.id.not_in(exclude))
    if before is not None:
        statement = statement.where(RouteRequest.created_at < before)
    return bool(await session.scalar(select(statement)))


MAX_QUEUE = 200  # requests read for one offer; the oldest are offered first


@dataclass(frozen=True)
class QueueEntry:
    request: RouteRequest
    position: int | None  # 1 is whom the offer picks; None when it can't be offered
    offered_before: bool
    not_offerable: NotOfferable | None
    key: QueueKey


@dataclass(frozen=True)
class _QueueTerms:
    """The offer queue's rules for one route, shared by the offer and the queue's list so
    the two can never disagree."""

    open_offer: ColumnElement[bool]  # an open offer in time, on any route
    passed_over: ColumnElement[bool]  # offered a seat on this route that lapsed
    not_offerable: ColumnElement[str | None]
    group: ColumnElement[int]


def _queue_terms(route_id: uuid.UUID) -> _QueueTerms:
    """A request a seat can be offered to: an adult's (seats are offered to adults) with
    no open offer. Oldest first, except that one already offered a seat
    on this route that expired or was withdrawn goes behind the others (the seat passes to
    the next request)."""
    in_time = SeatOffer.expires_at > func.now()
    open_offer = exists().where(
        SeatOffer.request_id == RouteRequest.id, SeatOffer.status == OfferStatus.OPEN, in_time
    )
    passed_over = exists().where(
        SeatOffer.request_id == RouteRequest.id,
        SeatOffer.route_id == route_id,
        or_(
            SeatOffer.status.in_((OfferStatus.EXPIRED, OfferStatus.WITHDRAWN)),
            and_(SeatOffer.status == OfferStatus.OPEN, ~in_time),
        ),
    )
    not_offerable = case(
        (RouteRequest.rider_type != RiderType.ADULT, literal(NotOfferable.STUDENT.value)),
        (open_offer, literal(NotOfferable.OPEN_OFFER.value)),
        else_=literal(None, String),
    )
    group = case(
        (or_(RouteRequest.rider_type != RiderType.ADULT, open_offer), NOT_OFFERABLE),
        (passed_over, PASSED_OVER),
        else_=NEXT,
    )
    return _QueueTerms(open_offer, passed_over, not_offerable, group)


async def waiting_without_offer(
    session: AsyncSession, route_id: uuid.UUID, *, also: uuid.UUID | None = None
) -> list[RouteRequest]:
    """The requests a free seat on this route can be offered to (GZ-040), in the queue's
    order (`_queue_terms`): the first MAX_QUEUE, and `also` (the one staff name) wherever
    it stands, last when beyond them.

    Locked (`FOR UPDATE`, after the route's lock) in id order, whatever the queue's order:
    a request can wait for two routes, and two offers locking it in different orders could
    deadlock. The lock re-checks each row, so one closed meanwhile drops out."""
    terms = _queue_terms(route_id)
    waiting = (*waiting_for(route_id), terms.group != NOT_OFFERABLE)
    queue = await session.scalars(
        select(RouteRequest.id)
        .where(*waiting)
        .order_by(terms.group, RouteRequest.created_at, RouteRequest.id)
        .limit(MAX_QUEUE)
    )
    place = {request_id: index for index, request_id in enumerate(queue)}
    wanted = [*place, *([also] if also is not None and also not in place else [])]
    locked = await session.scalars(
        select(RouteRequest)
        .where(RouteRequest.id.in_(wanted), *waiting)
        .order_by(RouteRequest.id)
        .with_for_update()
    )
    return sorted(locked, key=lambda request: place.get(request.id, len(place)))


async def offer_queue(
    session: AsyncSession, route_id: uuid.UUID, *, after: QueueKey | None, limit: int
) -> list[QueueEntry]:
    """Up to `limit` requests waiting for this route, after `after`, in the queue's order
    (`_queue_terms`): those a seat can be offered to, numbered from 1 (whom the offer
    picks), then those it can't, oldest first, with the reason. One query, numbered over
    the whole queue, so a page's positions continue the one before (as of each page's read:
    an offer made between two pages moves its request to the end).

    Indexes: the requests through `waiting_for`'s (`ix_route_requests_route_id`,
    `ix_route_requests_matched_route_id`); offers through `uq_seat_offers_request_id_open`
    and `ix_seat_offers_route_id_created_at`."""
    terms = _queue_terms(route_id)
    ranked = (
        select(
            RouteRequest.id.label("id"),
            RouteRequest.created_at.label("created_at"),
            terms.group.label("grp"),
            terms.passed_over.label("offered_before"),
            terms.not_offerable.label("not_offerable"),
        )
        .where(*waiting_for(route_id))
        .subquery("ranked")
    )
    order = (ranked.c.grp, ranked.c.created_at, ranked.c.id)
    numbered = select(ranked, func.row_number().over(order_by=order).label("number")).subquery(
        "numbered"
    )
    statement = (
        select(
            RouteRequest,
            numbered.c.number,
            numbered.c.grp,
            numbered.c.offered_before,
            numbered.c.not_offerable,
        )
        .join(numbered, numbered.c.id == RouteRequest.id)
        .options(*_WITH_PLACES)
    )
    key = tuple_(numbered.c.grp, numbered.c.created_at, numbered.c.id)
    if after is not None:
        statement = statement.where(key > tuple_(after.group, after.created_at, after.id))
    rows = await session.execute(
        statement.order_by(numbered.c.grp, numbered.c.created_at, numbered.c.id).limit(limit)
    )
    return [
        QueueEntry(
            request=request,
            position=None if group == NOT_OFFERABLE else number,
            offered_before=offered_before,
            not_offerable=None if reason is None else NotOfferable(reason),
            key=QueueKey(group, request.created_at, request.id),
        )
        for request, number, group, offered_before, reason in rows
    ]


def waiting_for(route_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """Live, open, and for this route: asked for it (`route`) or matched to it."""
    return (
        RouteRequest.not_deleted(),
        _OPEN,
        or_(
            and_(RouteRequest.kind == RequestKind.ROUTE, RouteRequest.route_id == route_id),
            RouteRequest.matched_route_id == route_id,
        ),
    )


async def open_request_exists(
    session: AsyncSession, asked: Asked, *, except_id: uuid.UUID | None
) -> bool:
    """Another live, open request asks for the same (the unique index's rule, so the check
    and the index never disagree)."""
    statement = exists().where(*_same_open(asked))
    if except_id is not None:
        statement = statement.where(RouteRequest.id != except_id)
    return bool(await session.scalar(select(statement)))


async def same_open_request(session: AsyncSession, asked: Asked) -> RouteRequest | None:
    """The live, open request asking for the same, locked (`FOR UPDATE`): GZ-032 links a
    request staff recorded to the rider who then signed up."""
    return await session.scalar(select(RouteRequest).where(*_same_open(asked)).with_for_update())


def _same_open(asked: Asked) -> tuple[ColumnElement[bool], ...]:
    stored = _keys(
        RouteRequest.contact_phone,
        RouteRequest.kind,
        RouteRequest.route_id,
        _area_key(RouteRequest.from_area_id, RouteRequest.from_area_name),
        _area_key(RouteRequest.to_area_id, RouteRequest.to_area_name),
        RouteRequest.rider_name,
    )
    new = _keys(
        # Typed: an untyped None and a UUID would compile alike, and the driver's statement
        # cache would then send a UUID where it first saw text.
        literal(asked.contact_phone, String),
        literal(asked.kind.value, String),
        literal(asked.route_id, Uuid),
        _area_key(literal(asked.from_area_id, Uuid), literal(asked.from_area_name, String)),
        _area_key(literal(asked.to_area_id, Uuid), literal(asked.to_area_name, String)),
        literal(asked.rider_name, String),
    )
    return (
        RouteRequest.not_deleted(),
        _OPEN,
        *(column == value for column, value in zip(stored, new, strict=True)),
    )


def live_requests(
    *,
    status: RequestStatus | None,
    kind: RequestKind | None,
    open_only: bool | None,
    route_id: uuid.UUID | None,
) -> Select[RouteRequest]:
    """The filtered requests with what they name, unordered: `paginate` orders and bounds."""
    statement = select(RouteRequest).options(*_WITH_PLACES).where(RouteRequest.not_deleted())
    if status is not None:
        statement = statement.where(RouteRequest.status == status)
    if kind is not None:
        statement = statement.where(RouteRequest.kind == kind)
    if open_only is not None:
        statement = statement.where(_OPEN if open_only else ~_OPEN)
    if route_id is not None:  # asked for, or matched to
        statement = statement.where(
            or_(RouteRequest.route_id == route_id, RouteRequest.matched_route_id == route_id)
        )
    return statement


async def names_area(session: AsyncSession, area_id: uuid.UUID) -> bool:
    """A live, open request names this area at either end (indexed both ways)."""
    statement = exists().where(
        RouteRequest.not_deleted(),
        _OPEN,
        or_(RouteRequest.from_area_id == area_id, RouteRequest.to_area_id == area_id),
    )
    return bool(await session.scalar(select(statement)))


async def names_route(session: AsyncSession, route_id: uuid.UUID) -> bool:
    """A live, open request asks for this route or was matched to it."""
    statement = exists().where(
        RouteRequest.not_deleted(),
        _OPEN,
        or_(RouteRequest.route_id == route_id, RouteRequest.matched_route_id == route_id),
    )
    return bool(await session.scalar(select(statement)))


async def area_pair_demand(session: AsyncSession, limit: int) -> list[PairDemand]:
    """Open new-areas requests not matched yet, per area pair (a name not listed yet
    compared ignoring case, shown as first recorded), busiest first, at most `limit`."""
    first = (RouteRequest.created_at, RouteRequest.id)
    from_key = (RouteRequest.from_area_id, func.lower(RouteRequest.from_area_name))
    to_key = (RouteRequest.to_area_id, func.lower(RouteRequest.to_area_name))
    rows = await session.execute(
        select(
            RouteRequest.from_area_id,
            func.array_agg(aggregate_order_by(RouteRequest.from_area_name, *first))[1],
            RouteRequest.to_area_id,
            func.array_agg(aggregate_order_by(RouteRequest.to_area_name, *first))[1],
            func.count(),
        )
        .where(
            RouteRequest.not_deleted(),
            RouteRequest.kind == RequestKind.NEW_AREAS,
            RouteRequest.status == RequestStatus.NEW,
        )
        .group_by(*from_key, *to_key)
        # The pair itself breaks ties: the same order every call, so the cut at `limit` too.
        .order_by(func.count().desc(), func.min(RouteRequest.created_at), *from_key, *to_key)
        .limit(limit)
    )
    return [PairDemand(*row) for row in rows]


async def route_demand(session: AsyncSession, limit: int) -> list[tuple[uuid.UUID, int]]:
    """Open requests per route, each once: the route it was matched to, else the one asked
    for; busiest first, at most `limit`."""
    target = func.coalesce(RouteRequest.matched_route_id, RouteRequest.route_id)
    rows = await session.execute(
        select(target, func.count())
        .where(RouteRequest.not_deleted(), _OPEN, target.is_not(None))
        .group_by(target)
        .order_by(func.count().desc(), func.min(RouteRequest.created_at), target)
        .limit(limit)
    )
    return [(route_id, count) for route_id, count in rows]


async def areas_by_id(session: AsyncSession, area_ids: set[uuid.UUID]) -> dict[uuid.UUID, Area]:
    if not area_ids:
        return {}
    return {
        area.id: area for area in await session.scalars(select(Area).where(Area.id.in_(area_ids)))
    }


async def routes_by_id(session: AsyncSession, route_ids: list[uuid.UUID]) -> dict[uuid.UUID, Route]:
    if not route_ids:
        return {}
    return {
        route.id: route
        for route in await session.scalars(select(Route).where(Route.id.in_(route_ids)))
    }
