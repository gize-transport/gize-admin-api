"""The overview's body (GZ-064)."""

from pydantic import BaseModel, ConfigDict, Field

from gize_api.core.schedule import LocalDate


class _Counts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RouteCounts(_Counts):
    draft: int
    forming: int
    active: int
    paused: int
    ready_to_launch: int = Field(
        description="Forming routes whose riders reserved for the next launch day reach "
        "their launch minimum: the routes `routes_list` lists with `ready_to_launch=true`."
    )
    active_without_driver: int


class RiderCounts(_Counts):
    total: int
    riding: int = Field(description="Holding an active seat today.")
    reserved: int = Field(description="Holding a reservation on a forming route.")
    starting: int = Field(
        description="Holding an active seat that starts after today (placed from a later "
        "1st, or on a route launched for one), and not riding today. They count as riding "
        "from that day."
    )


class OpenCount(_Counts):
    open: int


class ActiveCount(_Counts):
    active: int


class Overview(BaseModel):
    """Counts for the admin home page, live records only, as of `as_of` (Addis Ababa)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: LocalDate
    routes: RouteCounts
    riders: RiderCounts
    route_requests: OpenCount = Field(description="New or matched.")
    seat_offers: OpenCount = Field(
        description="Open, within their 48 hours, their request still waiting: the seats "
        "offers hold."
    )
    drivers: ActiveCount
