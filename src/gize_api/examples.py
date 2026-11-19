"""The contract's success examples (GZ-061): one per response model, shown on every 2xx
response that returns it (core/contract.py; a page shows one item).

Each is the model itself, so a field renamed, added or tightened fails the contract's
tests rather than leaving the frontend team a stale example; tests/test_contract_examples.py
checks each one reads back exactly as the API would send it. Made-up people, phones and plates.
"""

from functools import cache
from typing import Any

from pydantic import BaseModel

from gize_api.areas.schemas import AreaRead
from gize_api.audit_log.schemas import AuditEntryRead
from gize_api.catalogue.schemas import PublicRoute
from gize_api.companies.schemas import CompanyRead
from gize_api.corridors.schemas import AreaRef, CorridorRead
from gize_api.drivers.schemas import DriverRead
from gize_api.launches.schemas import LaunchMessages
from gize_api.offers.schemas import OfferQueueItem, OfferRead, PublicOfferAccepted
from gize_api.overview.schemas import Overview
from gize_api.registrations.schemas import AdultRegistered
from gize_api.riders.schemas import RiderListItem, RiderRead
from gize_api.route_requests.schemas import RouteRequestRead, RouteRequestSummary
from gize_api.routes.schemas import RouteLaunchMonths, RouteRead
from gize_api.staff.schemas import StaffMe, StaffRead
from gize_api.verification.schemas import PhoneVerified, VerificationSent

CMC = {"id": "0b6f1d2e-4c3a-4f5e-9a8b-1c2d3e4f5a01", "name": "CMC"}
BOLE = {"id": "0b6f1d2e-4c3a-4f5e-9a8b-1c2d3e4f5a02", "name": "Bole"}
MEGENAGNA = {"id": "0b6f1d2e-4c3a-4f5e-9a8b-1c2d3e4f5a03", "name": "Megenagna"}
CORRIDOR_ID = "1c7a2e3f-5d4b-4a6f-8b9c-2d3e4f5a6b01"
ROUTE = {"id": "2d8b3f4a-6e5c-4b7a-9c0d-3e4f5a6b7c01", "code": "CMC-BOLE-1"}
RIDER_ID = "3e9c4a5b-7f6d-4c8b-8d1e-4f5a6b7c8d01"
REQUEST_ID = "4fad5b6c-8a7e-4d9c-9e2f-5a6b7c8d9e01"
OFFER_ID = "5abe6c7d-9b8f-4e0d-8f3a-6b7c8d9e0f01"
DRIVER = {"id": "6bcf7d8e-0c9a-4f1e-9a4b-7c8d9e0f1a01", "full_name": "Dawit Alemu"}
STAFF_ID = "7cd08e9f-1dab-4a2f-8b5c-8d9e0f1a2b01"
COMPANY = {"id": "8de19fa0-2ebc-4b3a-9c6d-9e0f1a2b3c01", "name": "Lucy Software PLC"}
CREATED = "2026-10-01T06:30:00Z"
UPDATED = "2026-11-14T09:15:00Z"
SEAT = {
    "route_id": ROUTE["id"],
    "route_code": ROUTE["code"],
    "start_date": "2026-11-01",
    "end_date": None,
    "status": "active",
}
RIDER = {
    "id": RIDER_ID,
    "full_name": "Selam Tesfaye",
    "rider_type": "adult",
    "source": "staff",
    "plan": "round_trip",
    "saturday_addon": False,
    "home_area": CMC,
    "destination_area": BOLE,
    "company": None,
    "seat": SEAT,
    "next_seat": None,
    "open_requests": 0,
    "created_at": CREATED,
    "updated_at": UPDATED,
}

REQUEST = {
    "id": REQUEST_ID,
    "kind": "route",
    "status": "new",
    "source": "app",
    "rider_name": "Meron Haile",
    "rider_type": "adult",
    "contact_name": "Meron Haile",
    "contact_phone": "+251911000124",
    "school_name": None,
    "route": ROUTE,
    "matched_route": None,
    "from_area": None,
    "from_area_name": None,
    "to_area": None,
    "to_area_name": None,
    "preferred_morning_time": None,
    "preferred_return_time": None,
    "notes": None,
    "decline_reason": None,
    "created_at": CREATED,
    "updated_at": UPDATED,
}


@cache
def examples() -> tuple[BaseModel, ...]:
    """Built on first use (the contract's), never at import: a validator that one day
    depends on today's date can fail a test, not the API's startup."""
    return (
        AreaRead.model_validate({**BOLE, "created_at": CREATED, "updated_at": UPDATED}),
        AreaRef.model_validate(BOLE),
        CorridorRead.model_validate(
            {
                "id": CORRIDOR_ID,
                "origin_area": CMC,
                "destination_area": BOLE,
                "created_at": CREATED,
                "updated_at": UPDATED,
            }
        ),
        CompanyRead.model_validate(
            {
                **COMPANY,
                "contact_name": "Hanna Bekele",
                "contact_phone": "+251911000101",
                "contact_email": "transport@example.com",
                "billing": "postpaid",
                "is_active": True,
                "notes": None,
                "created_at": CREATED,
                "updated_at": UPDATED,
            }
        ),
        RouteRead.model_validate(
            {
                **ROUTE,
                "route_type": "public",
                "status": "active",
                "corridor": {"id": CORRIDOR_ID, "origin_area": CMC, "destination_area": BOLE},
                "served_areas": [MEGENAGNA],
                "morning_time": "06:30",
                "morning_arrival_time": "07:20",
                "return_time": "17:30",
                "return_arrival_time": "18:30",
                "seats": 14,
                "seats_taken": {"this_month": 12, "next_month": 13},
                "seats_free": {"this_month": 2, "next_month": 1},
                "reserved": 0,
                "launch_minimum": 10,
                "prices": {"round_trip": 3200, "one_way": 1800, "saturday": 600},
                "company": None,
                "school_name": None,
                "driver": {**DRIVER, "plate": "AA 3-B12345"},
                "copied_from_route_id": None,
                "status_changes": [
                    {"to": "paused", "ready": True, "launch_date": None, "blocked_by": []}
                ],
                "created_at": CREATED,
                "updated_at": UPDATED,
            }
        ),
        RouteLaunchMonths.model_validate(
            {
                "status": "forming",
                "launch_minimum": 10,
                "months": [
                    {"launch_date": "2026-12-01", "reserved": 11, "ready": True},
                    {"launch_date": "2027-01-01", "reserved": 11, "ready": True},
                ],
            }
        ),
        RiderRead.model_validate({**RIDER, "phone": "+251911000123"}),
        RiderListItem.model_validate({**RIDER, "phone_masked": "+2519****0123"}),
        RouteRequestRead.model_validate(REQUEST),
        OfferQueueItem.model_validate(
            {"position": 1, "offered_before": False, "not_offerable": None, "request": REQUEST}
        ),
        RouteRequestSummary.model_validate(
            {
                "routes": [
                    {"route": ROUTE, "status": "active", "open": 3, "seats_free_next_month": 1}
                ],
                "routes_truncated": False,
                "area_pairs": [
                    {
                        "from_area": CMC,
                        "from_area_name": None,
                        "to_area": None,
                        "to_area_name": "Kality",
                        "open": 2,
                    }
                ],
                "area_pairs_truncated": False,
            }
        ),
        OfferRead.model_validate(
            {
                "id": OFFER_ID,
                "route_id": ROUTE["id"],
                "route_code": ROUTE["code"],
                "request_id": REQUEST_ID,
                "rider_name": "Meron Haile",
                "start_date": "2026-12-01",
                "status": "open",
                "expires_at": "2026-11-16T09:15:00Z",
                "skip_reason": None,
                "rider_id": None,
                "sms_status": "sent",
                "created_at": UPDATED,
                "updated_at": UPDATED,
            }
        ),
        PublicOfferAccepted.model_validate(
            {"status": "accepted", "route_code": ROUTE["code"], "start_date": "2026-12-01"}
        ),
        PublicRoute.model_validate(
            {
                **ROUTE,
                "origin_area": CMC,
                "destination_area": BOLE,
                "served_areas": [MEGENAGNA],
                "morning_time": "06:30",
                "morning_arrival_time": "07:20",
                "return_time": "17:30",
                "return_arrival_time": "18:30",
                "prices": {"round_trip": 3200, "one_way": 1800, "saturday": 600},
                "status": "active",
                "full": False,
            }
        ),
        AdultRegistered.model_validate(
            {
                "outcome": "reserved",
                "rider_id": RIDER_ID,
                "route_id": ROUTE["id"],
                "route_request_id": None,
                "start_date": "2026-12-01",
            }
        ),
        VerificationSent.model_validate(
            {
                "verification_id": "9ef2a0b1-3fcd-4c4b-8d7e-0f1a2b3c4d01",
                "expires_at": "2026-11-15T09:25:00Z",
            }
        ),
        PhoneVerified.model_validate(
            {
                "verified_phone_token": "example.example.example",
                "expires_at": "2026-11-15T09:50:00Z",
            }
        ),
        LaunchMessages.model_validate(
            {
                "launch_date": "2026-12-01",
                "total": 2,
                "sent": 1,
                "sending": 0,
                "unconfirmed": 0,
                "failed": 1,
                "pending": 0,
                "riders": [
                    {
                        "rider_id": RIDER_ID,
                        "full_name": "Selam Tesfaye",
                        "start_date": "2026-12-01",
                        "status": "failed",
                        "attempts": 3,
                    },
                ],
            }
        ),
        DriverRead.model_validate(
            {
                **DRIVER,
                "phone": "+251911000201",
                "address": "Bole, Woreda 03",
                "plate": "AA 3-B12345",
                "is_active": True,
                "created_at": CREATED,
                "updated_at": UPDATED,
            }
        ),
        StaffRead.model_validate(
            {
                "id": STAFF_ID,
                "email": "operations@example.com",
                "full_name": "Tigist Mengistu",
                "role": "staff",
                "is_active": True,
                "first_signed_in_at": CREATED,
                "created_at": CREATED,
                "updated_at": UPDATED,
            }
        ),
        StaffMe.model_validate(
            {
                "id": STAFF_ID,
                "email": "operations@example.com",
                "full_name": "Tigist Mengistu",
                "role": "staff",
            }
        ),
        AuditEntryRead.model_validate(
            {
                "id": "a0f3b1c2-4ade-4d5c-9e8f-1a2b3c4d5e01",
                "created_at": UPDATED,
                "actor_staff_id": STAFF_ID,
                "action": "driver.updated",
                "entity_type": "driver",
                "entity_id": DRIVER["id"],
                "before": {"is_active": True},
                "after": {"is_active": False},
                "request_id": "4b1f0c3e9a2d4e7f8c6b5a4d3e2f1a0b",
            }
        ),
        Overview.model_validate(
            {
                "as_of": "2026-11-15",
                "routes": {
                    "draft": 1,
                    "forming": 3,
                    "active": 12,
                    "paused": 0,
                    "ready_to_launch": 2,
                    "active_without_driver": 1,
                },
                "riders": {"total": 180, "riding": 140, "reserved": 24, "starting": 10},
                "route_requests": {"open": 9},
                "seat_offers": {"open": 2},
                "drivers": {"active": 13},
            }
        ),
    )


def contract_examples() -> dict[str, Any]:
    """Each example as the API sends it, by its contract component name."""
    return {
        type(example).__name__: example.model_dump(mode="json", by_alias=True)
        for example in examples()
    }
