"""Every model, imported in one place so Alembic compares the whole schema.

Import each domain package's models module here when it is created, e.g.
`from gize_api.riders import models as riders_models`. A test fails if one is missing.
"""

from gize_api.areas import models as area_models  # gize.areas
from gize_api.companies import models as company_models  # gize.companies
from gize_api.core import audit, idempotency  # gize.audit_log, gize.idempotency_keys
from gize_api.core.models import Base
from gize_api.corridors import models as corridor_models  # gize.corridors
from gize_api.drivers import models as driver_models  # gize.drivers
from gize_api.offers import models as offer_models  # gize.seat_offers
from gize_api.riders import models as rider_models  # gize.riders
from gize_api.route_requests import models as route_request_models  # gize.route_requests
from gize_api.routes import models as route_models  # gize.routes, gize.route_assignments
from gize_api.sms import models as sms_models  # gize.sms_messages
from gize_api.staff import models as staff_models  # gize.staff
from gize_api.verification import models as verification_models  # gize.phone_verifications

__all__ = [
    "Base",
    "area_models",
    "audit",
    "company_models",
    "corridor_models",
    "driver_models",
    "idempotency",
    "offer_models",
    "rider_models",
    "route_models",
    "route_request_models",
    "sms_models",
    "staff_models",
    "verification_models",
]
