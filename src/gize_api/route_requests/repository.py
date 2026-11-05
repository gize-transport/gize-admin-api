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
    cast,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from gize_api.areas.models import Area
from gize_api.offers.models import OfferStatus, SeatOffer
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


async def waiting_without_offer(
    session: AsyncSession, route_id: uuid.UUID, *, also: uuid.UUID | None = None
) -> list[RouteRequest]:
    """Adults' requests waiting for this route with no open offer (GZ-040): the queue a
    free seat is offered from. Oldest first, except that a request already offered a seat
    on this route that expired or was withdrawn goes behind the others (the seat passes to
    the next request). The first MAX_QUEUE, and `also` (the one staff name) wherever it
    stands, last when beyond them.

    Locked (`FOR UPDATE`, after the route's lock) in id order, whatever the queue's order:
    a request can wait for two routes, and two offers locking it in different orders could
    deadlock. The lock re-checks each row, so one closed meanwhile drops out."""
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
    waiting = (*waiting_for(route_id), RouteRequest.rider_type == RiderType.ADULT, ~open_offer)
    queue = await session.scalars(
        select(RouteRequest.id)
        .where(*waiting)
        .order_by(passed_over, RouteRequest.created_at, RouteRequest.id)
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
