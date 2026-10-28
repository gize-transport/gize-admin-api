"""`/health/live` and `/health/ready`: no auth, no versions or internals, never cached.

Hidden from the API schema: they are for the load balancer, not the frontend. Probes are
neither traced nor access-logged when they succeed, so each one is counted instead.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gize_api.core.errors import ErrorCode, problem_response
from gize_api.health.readiness import Readiness

RETRY_AFTER_SECONDS = "5"
_NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(prefix="/health", include_in_schema=False)


@router.api_route("/live", methods=["GET", "HEAD"])
async def live(request: Request) -> Response:
    """The process is up and serving. Checks nothing else, so a dependency outage never
    makes the host restart healthy instances."""
    return _counted(request, "live", JSONResponse({"status": "ok"}, headers=_NO_STORE))


@router.api_route("/ready", methods=["GET", "HEAD"])
async def ready(request: Request) -> Response:
    """Postgres answers within its timeout (Redis is checked and reported, see readiness)."""
    readiness: Readiness = request.app.state.readiness
    if await readiness.is_ready():
        return _counted(request, "ready", JSONResponse({"status": "ok"}, headers=_NO_STORE))
    return _counted(
        request,
        "ready",
        problem_response(
            ErrorCode.SERVICE_UNAVAILABLE,
            headers={"Retry-After": RETRY_AFTER_SECONDS, **_NO_STORE},
        ),
    )


def _counted(request: Request, endpoint: str, response: Response) -> Response:
    telemetry = getattr(request.app, "telemetry", None)
    if telemetry is not None:
        telemetry.record_health_probe(endpoint, response.status_code)
    return response
