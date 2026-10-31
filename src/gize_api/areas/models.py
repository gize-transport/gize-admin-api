"""Areas (GZ-026): one list of neighbourhoods and landmarks, so "Ayat" typed three ways is
one place. Corridors, the areas routes serve, requests and riders refer to areas by ID.

Names are kept as typed and unique ignoring case among live areas; a deleted area's name
can be used again. Areas are a short list (hundreds), so a name search scans it.
"""

from sqlalchemy import CheckConstraint, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.audit import AuditedEntity
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import SHORT_TEXT_MAX_LENGTH

AREA_NAME_UNIQUE = "uq_areas_name"

# Areas hold no personal data: every field by value.
AREA_AUDIT = AuditedEntity(
    type="area", fields=("name", "deleted_at"), visible=frozenset({"name", "deleted_at"})
)


class Area(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "areas"
    __table_args__ = (
        CheckConstraint("name = btrim(name) AND name <> ''", name="name"),
        Index(
            AREA_NAME_UNIQUE,
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_areas_created_at_id", "created_at", "id"),  # the list's order
    )

    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
