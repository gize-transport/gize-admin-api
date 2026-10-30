"""Every model, imported in one place so Alembic compares the whole schema.

Import each domain package's models module here when it is created, e.g.
`from gize_api.riders import models as riders_models`. A test fails if one is missing.
"""

from gize_api.areas import models as area_models  # gize.areas
from gize_api.core import audit, idempotency  # gize.audit_log, gize.idempotency_keys
from gize_api.core.models import Base
from gize_api.staff import models as staff_models  # gize.staff

__all__ = ["Base", "area_models", "audit", "idempotency", "staff_models"]
