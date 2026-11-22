"""GZ-084: the open seat offers across all routes (`GET /v1/offers`).

The overview's "open seat offers" count opens this list: the same offers, by the same rule
(open, within their 48 hours, their request still waiting for the route). The one running
out soonest comes first, so staff phone that rider first while SMS is paused; offers
running out at the same moment come by id. Items are a route's offer list's items: no
phone numbers (staff open the request for those).
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core.db import get_session
from gize_api.main import GizeAPI
from gize_api.offers.models import OfferStatus, SeatOffer
from gize_api.route_requests.models import RequestStatus
from gize_api.routes.models import RouteStatus
from gize_api.sms.models import SmsMessage, SmsPurpose, SmsStatus
from tests.factories import RiderFactory, RouteRequestFactory
from tests.routes.support import add_route
from tests.staff.support import add_admin, add_staff, signed_in
from tests.support import every_page

OFFERS = "/v1/offers"


async def _offer(
    session: AsyncSession,
    route: uuid.UUID,
    expires_in: timedelta,
    *,
    request: dict[str, Any] | None = None,
    since: datetime | None = None,
    **values: Any,  # noqa: ANN401
) -> str:
    """An offer on `route` to a new request for it, running out `expires_in` from `since`
    (now unless given)."""
    async with session.begin():
        asked = await RouteRequestFactory().create(
            session, kind="route", route_id=route, from_area_name=None, to_area_name=None,
            **(request or {}),
        )  # fmt: skip
        expires_at = (since or datetime.now(UTC)) + expires_in
        offer = SeatOffer(route_id=route, request_id=asked.id, start_date=date(2026, 12, 1),
                          expires_at=expires_at, **values)  # fmt: skip
        session.add(offer)
        await session.flush()
        return str(offer.id)


async def _overview_count(api: httpx.AsyncClient, member: uuid.UUID) -> int:
    response = await api.get("/v1/overview", headers=signed_in(member))
    count: int = response.json()["seat_offers"]["open"]
    return count


@pytest.fixture
async def member(db_session: AsyncSession) -> uuid.UUID:
    return (await add_staff(db_session)).id


async def test_open_offers_on_every_route_soonest_to_run_out_first(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    bole = await add_route(db_session, code="BOLE-01", status=RouteStatus.ACTIVE)
    cmc = await add_route(db_session, code="CMC-01", status=RouteStatus.FORMING)
    later = await _offer(db_session, bole, timedelta(hours=40))
    soonest = await _offer(db_session, cmc, timedelta(hours=1),
                           request={"rider_name": "Almaz Tesfaye"})  # fmt: skip
    middle = await _offer(db_session, bole, timedelta(hours=20))
    # Holding nothing: past its time though stored open, its request declined, withdrawn.
    await _offer(db_session, bole, -timedelta(hours=1))
    declined = {"status": RequestStatus.DECLINED, "decline_reason": "Moved."}
    await _offer(db_session, cmc, timedelta(hours=5), request=declined)
    await _offer(db_session, cmc, timedelta(hours=5), status=OfferStatus.WITHDRAWN)
    async with db_session.begin():
        rider = await RiderFactory().create(db_session)
    await _offer(db_session, bole, timedelta(hours=5), status=OfferStatus.ACCEPTED,
                 rider_id=rider.id)  # fmt: skip
    headers = signed_in(member)

    response = await api.get(OFFERS, headers=headers)
    assert response.status_code == 200, response.json()
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert [item["id"] for item in body["items"]] == [soonest, middle, later]
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert {key: first[key] for key in ("route_id", "route_code", "rider_name", "status")} == {
        "route_id": str(cmc), "route_code": "CMC-01", "rider_name": "Almaz Tesfaye",
        "status": "open",
    }  # fmt: skip
    assert set(first) == {
        "id", "route_id", "route_code", "request_id", "rider_name", "start_date", "status",
        "expires_at", "skip_reason", "rider_id", "sms_status", "created_at", "updated_at",
    }  # fmt: skip
    assert len(body["items"]) == await _overview_count(api, member)


async def test_each_offer_shows_its_text_message(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    """`failed` while SMS is paused: staff phone them."""
    route = await add_route(db_session, status=RouteStatus.ACTIVE)
    texted = await _offer(db_session, route, timedelta(hours=1))
    unsent = await _offer(db_session, route, timedelta(hours=2))
    async with db_session.begin():
        db_session.add(SmsMessage(dedup_key=f"offer:{texted}", purpose=SmsPurpose.SEAT_OFFER,
                                  to_phone="+251911000001", status=SmsStatus.FAILED,
                                  error="unavailable"))  # fmt: skip
    items = (await api.get(OFFERS, headers=signed_in(member))).json()["items"]
    assert [(item["id"], item["sms_status"]) for item in items] == [
        (texted, "failed"),
        (unsent, None),
    ]


async def test_pages_split_offers_running_out_together(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    route = await add_route(db_session, status=RouteStatus.ACTIVE)
    now = datetime.now(UTC)
    together = [await _offer(db_session, route, timedelta(hours=3), since=now) for _ in range(3)]
    first = await _offer(db_session, route, timedelta(hours=1))
    last = await _offer(db_session, route, timedelta(hours=47))
    listed = await every_page(api, OFFERS, signed_in(member))
    assert listed == [first, *sorted(together), last]


async def test_an_empty_list(api: httpx.AsyncClient, member: uuid.UUID) -> None:
    response = await api.get(OFFERS, headers=signed_in(member))
    assert (response.status_code, response.json()) == (200, {"items": [], "next_cursor": None})


def _cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"limit": "0"}, "VALIDATION_FAILED"),
        ({"limit": "101"}, "VALIDATION_FAILED"),
        ({"route_id": str(uuid.uuid4())}, "VALIDATION_FAILED"),
        ({"sort": "newest"}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
        ({"cursor": "x" * 1201}, "VALIDATION_FAILED"),
        # Another list's cursor (newest first), and a time without its zone.
        ({"cursor": _cursor(["2026-11-20T06:30:00+00:00", str(uuid.uuid4())])}, "INVALID_CURSOR"),
        ({"cursor": _cursor(["expires_at", "2026-11-20T06:30:00", str(uuid.uuid4())])},
         "INVALID_CURSOR"),
        ({"cursor": _cursor(["name", "Abebe", str(uuid.uuid4())])}, "INVALID_CURSOR"),
    ],
)  # fmt: skip
async def test_bad_parameters_are_422(
    api: httpx.AsyncClient, member: uuid.UUID, params: dict[str, str], code: str
) -> None:
    response = await api.get(OFFERS, params=params, headers=signed_in(member))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_every_active_staff_member_may_list(
    api: httpx.AsyncClient, db_session: AsyncSession, member: uuid.UUID
) -> None:
    admin = (await add_admin(db_session)).id
    for caller in (member, admin):
        assert (await api.get(OFFERS, headers=signed_in(caller))).status_code == 200


async def test_callers_must_be_signed_in_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await api.get(OFFERS)
    assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")
    gone = (await add_staff(db_session, is_active=False)).id
    for caller in (uuid.uuid4(), gone):
        assert (await api.get(OFFERS, headers=signed_in(caller))).status_code == 403


async def test_the_database_down_is_503(
    api: httpx.AsyncClient, api_app: GizeAPI, unreachable_sessions: AsyncSession
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.get(OFFERS, headers=signed_in(uuid.uuid4()))
    assert (response.status_code, response.json()["code"]) == (503, "SERVICE_UNAVAILABLE")
