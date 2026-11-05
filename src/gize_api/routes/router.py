"""/v1/routes: routes, created, listed, read, edited and moved through their statuses by
staff (GZ-021, GZ-024); prices set and routes deleted by admins (GZ-022, GZ-024)."""

import uuid
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.core.errors import ErrorCode, problem_responses
from gize_api.core.idempotency import IdempotentRequest, idempotent_request
from gize_api.core.security import CurrentStaff, current_staff, require_role, staff_router
from gize_api.launches import service as launches
from gize_api.launches.schemas import LaunchMessages
from gize_api.routes import service
from gize_api.routes.schemas import (
    RouteAreasSet,
    RouteCopy,
    RouteCreate,
    RouteDriverSet,
    RouteLaunchMonths,
    RouteListParams,
    RoutePage,
    RoutePricesSet,
    RouteRead,
    RouteStatusSet,
    RouteUpdate,
)
from gize_api.staff.models import StaffRole

router = staff_router(prefix="/v1/routes", tags=["routes"])

require_admin = require_role(StaffRole.ADMIN)


def route_list_params(params: Annotated[RouteListParams, Query()]) -> RouteListParams:
    _ = params.after  # a bad cursor is refused before any work
    return params


@router.post(
    "",
    operation_id="routes_create",
    summary="Add a route",
    status_code=201,
    response_model=RouteRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new route.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.ROUTE_CODE_TAKEN,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def create(
    route: RouteCreate,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """A `draft` route. Requires `Idempotency-Key`. 409 ROUTE_CODE_TAKEN: a live route has
    this code. A corridor or company that doesn't exist (or a company that isn't active) is
    a 422 on its field."""
    response = await service.create(session, caller, route, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/{route_id}/copies",
    operation_id="routes_copy",
    summary="Add a van: copy a route",
    status_code=201,
    response_model=RouteRead,
    responses={
        201: {
            "description": "Created",
            "headers": {
                "Location": {"description": "The new route.", "schema": {"type": "string"}}
            },
        },
        **problem_responses(
            ErrorCode.NOT_FOUND,
            ErrorCode.ROUTE_CODE_TAKEN,
            ErrorCode.ROUTE_NOT_READY,
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        ),
    },
)
async def copy(
    route_id: uuid.UUID,
    van: RouteCopy,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """A new route like this one, for when it's full: corridor, type, school or company,
    times, prices (copied, never changed here) and served areas, with its own code and
    seats, and its own launch minimum: required for public and student routes, refused for
    corporate ones (a 422 on `launch_minimum` otherwise). It starts `forming` when the
    original has both prices (corporate: `draft`), else `draft`, and names the original in
    `copied_from_route_id`. Requires `Idempotency-Key`.

    - 409 ROUTE_CODE_TAKEN: a live route has this code.
    - 409 ROUTE_NOT_READY: a corporate route whose company isn't active.
    """
    response = await service.copy(session, caller, route_id, van, idempotent)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "",
    operation_id="routes_list",
    summary="Routes, newest first",
    responses=problem_responses(ErrorCode.INVALID_CURSOR),
)
async def list_routes(
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    params: RouteListParams = Depends(route_list_params),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RoutePage:
    """With corridor, areas, company, and seats taken and free this month and next."""
    response.headers["Cache-Control"] = "no-store"
    return await service.list_routes(session, params)


@router.get(
    "/{route_id}",
    operation_id="routes_get",
    summary="One route",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def get(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get(session, route_id)


@router.get(
    "/{route_id}/launch-months",
    operation_id="routes_launch_months",
    summary="The months a route can launch",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def launch_months(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteLaunchMonths:
    """Every `launch_date` a launch may name (the 1st of each month, from the earliest to a
    year ahead), with the riders reserved for it and whether a launch that day would be
    accepted now (`ready`: the route is forming and they reach its launch minimum). Other
    routes show their reservations, never ready. A snapshot: a launch can still be refused
    if riders leave meanwhile."""
    response.headers["Cache-Control"] = "no-store"
    return await service.launch_months(session, route_id)


@router.patch(
    "/{route_id}",
    operation_id="routes_update",
    summary="Change a route",
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.ROUTE_CODE_TAKEN,
        ErrorCode.ROUTE_HAS_PRICES,
        ErrorCode.ROUTE_HAS_REQUESTS,
        ErrorCode.DRIVER_BUSY,
        ErrorCode.ROUTE_HAS_RIDERS,
        ErrorCode.SEATS_BELOW_LAUNCH_MINIMUM,
        ErrorCode.SEATS_BELOW_TAKEN,
    ),
)
async def update(
    route_id: uuid.UUID,
    change: RouteUpdate,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """Leave out what stays. The type is fixed.

    - 409 ROUTE_HAS_RIDERS: corridor, school or company while riders hold seats.
    - 409 ROUTE_HAS_REQUESTS, ROUTE_HAS_PRICES: corridor or school once open route requests
      name the route, or once it has prices (to serve somewhere else, add a route).
    - 409 SEATS_BELOW_TAKEN: fewer seats than are taken on some day from today.
    - 409 SEATS_BELOW_LAUNCH_MINIMUM: the launch minimum above the stored seats, or seats
      below the stored minimum (both in one request and in disagreement: 422).
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.update(session, caller, route_id, change)


@router.put(
    "/{route_id}/prices",
    operation_id="routes_set_prices",
    summary="Set a route's prices (admins only)",
    responses=problem_responses(
        ErrorCode.NOT_FOUND, ErrorCode.ROUTE_HAS_NO_PRICES, ErrorCode.ROUTE_HAS_RIDERS
    ),
)
async def set_prices(
    route_id: uuid.UUID,
    prices: RoutePricesSet,
    response: Response,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """Round trip and one-way, whole birr a month; Saturday add-on on public routes only
    (left out or null: none). Staff can't change prices through any endpoint.

    - 409 ROUTE_HAS_NO_PRICES: a corporate route (billed per company).
    - 409 ROUTE_HAS_RIDERS: clearing the Saturday price while a rider holding a seat has
      the add-on (GZ-032).

    A malformed body is a 422 before the route is looked at (so before any 404 or 409).
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.set_prices(session, caller, route_id, prices)


@router.put(
    "/{route_id}/driver",
    operation_id="routes_set_driver",
    summary="Set who drives a route",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.DRIVER_BUSY),
)
async def set_driver(
    route_id: uuid.UUID,
    body: RouteDriverSet,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """`driver_id`: an active driver, or null for none (GZ-053). A driver may drive several
    routes whose runs don't overlap in time.

    - 409 DRIVER_BUSY: one of their other routes runs at the same time.
    - 422 on `driver_id`: unknown, deleted or inactive.
    """
    response.headers["Cache-Control"] = "no-store"
    return await service.set_driver(session, caller, route_id, body)


@router.put(
    "/{route_id}/areas",
    operation_id="routes_set_areas",
    summary="Set the areas a route serves",
    responses=problem_responses(ErrorCode.NOT_FOUND, ErrorCode.ROUTE_HAS_RIDERS),
)
async def set_areas(
    route_id: uuid.UUID,
    areas: RouteAreasSet,
    response: Response,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """The areas the route passes between A and B, where riders board or get off, in order
    (no times: a route is A to B). The whole list at once; empty clears it. Not A or B
    themselves, each once, live areas, at most 20 (a 422 on `area_ids` otherwise).

    - 409 ROUTE_HAS_RIDERS: the list drops an area where a rider holding a seat boards
      (GZ-032)."""
    response.headers["Cache-Control"] = "no-store"
    return await service.set_areas(session, caller, route_id, areas)


@router.put(
    "/{route_id}/status",
    operation_id="routes_set_status",
    summary="Move a route to another status",
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.INVALID_STATUS_CHANGE,
        ErrorCode.NOT_ENOUGH_RIDERS,
        ErrorCode.ROUTE_HAS_RIDERS,
        ErrorCode.ROUTE_NOT_READY,
    ),
)
async def set_status(
    route_id: uuid.UUID,
    change: RouteStatusSet,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RouteRead:
    """Public and student routes: `draft` → `forming` → `active` ⇄ `paused`, and `forming`
    → `draft`. Corporate routes: `draft` → `active` ⇄ `paused`. The status a route already
    has changes nothing.

    Launching (`forming` → `active`) takes `launch_date`, the 1st of a month from today to
    a year ahead (a 422 on `launch_date` otherwise, or when another move sends one). Riders
    reserved for that month or earlier must reach the launch minimum; they all start that
    day, and later reservations from their own date.

    - 409 INVALID_STATUS_CHANGE: any other move.
    - 409 ROUTE_NOT_READY: forming without round-trip and one-way prices; a corporate
      route starting while its company isn't active.
    - 409 NOT_ENOUGH_RIDERS: fewer riders reserved for the launch month than the minimum.
    - 409 ROUTE_HAS_RIDERS: back to draft while someone is reserved (today or later).

    After a launch, each rider it started gets an SMS, sent after this answer (GZ-041;
    see `GET /launch-messages`). A repeated launch sends nothing more.
    """
    response.headers["Cache-Control"] = "no-store"
    route = await service.set_status(session, caller, route_id, change)
    if change.launch_date is not None:
        background.add_task(launches.send_in_background, request.app, route_id, retry_failed=False)
    return route


@router.get(
    "/{route_id}/launch-messages",
    operation_id="routes_launch_messages",
    summary="The SMS a route's launch sent its riders",
    responses=problem_responses(ErrorCode.NOT_FOUND),
)
async def launch_messages(
    route_id: uuid.UUID,
    response: Response,
    _: CurrentStaff = Depends(current_staff),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> LaunchMessages:
    """Each rider the latest launch started, and their SMS: `sent` (the gateway took it),
    `sending` (right now), `unconfirmed` (no answer from the gateway: it may have arrived),
    `failed` or `pending` (not tried yet; both resent by `POST .../retry`). Check it after
    a launch: a restart midway leaves the rest `pending`. A route never launched has none."""
    response.headers["Cache-Control"] = "no-store"
    return await launches.status(session, route_id)


@router.post(
    "/{route_id}/launch-messages/retry",
    operation_id="routes_retry_launch_messages",
    summary="Resend a launch's failed SMS",
    status_code=202,
    response_model=LaunchMessages,
    responses=problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        ErrorCode.IDEMPOTENCY_KEY_MISMATCH,
        ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
    ),
)
async def retry_launch_messages(
    route_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    caller: CurrentStaff = Depends(current_staff),  # noqa: B008
    idempotent: IdempotentRequest = Depends(idempotent_request),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Requires `Idempotency-Key` (a retried request replays, starting nothing more).
    Sends, after this answer, each `failed` or `pending` message again: never one `sent`,
    `sending`, or `unconfirmed` (it may have arrived: call the rider if unsure). 202 with
    the messages as they were."""
    response, started = await launches.retry(session, caller, route_id, idempotent)
    if started:
        background.add_task(launches.send_in_background, request.app, route_id, retry_failed=True)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete(
    "/{route_id}",
    operation_id="routes_delete",
    summary="Delete a route (admins only)",
    status_code=204,
    response_class=Response,
    responses=problem_responses(
        ErrorCode.NOT_FOUND, ErrorCode.ROUTE_HAS_REQUESTS, ErrorCode.ROUTE_HAS_RIDERS
    ),
)
async def delete(
    route_id: uuid.UUID,
    caller: CurrentStaff = Depends(require_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Soft delete: hidden from lists, its code free again; history keeps it.

    - 409 ROUTE_HAS_RIDERS: anyone is reserved or rides today or later.
    - 409 ROUTE_HAS_REQUESTS: an open route request asks for it or was matched to it.
    """
    await service.delete(session, caller, route_id)
    return Response(status_code=204)
