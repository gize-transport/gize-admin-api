"""Corridors (GZ-020): a pair of areas, from (`origin`) and to (`destination`). Routes run a
corridor, and route requests are counted per corridor's pair of areas.

Each pair exists once among live corridors; the way back is another corridor. A corridor
holds no personal data: every field is audited by value.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gize_api.areas.models import Area
from gize_api.core.audit import AuditedEntity
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

CORRIDOR_AREAS_UNIQUE = "uq_corridors_areas"

CORRIDOR_AUDIT = AuditedEntity(
    type="corridor",
    fields=("origin_area_id", "destination_area_id", "deleted_at"),
    visible=frozenset({"origin_area_id", "destination_area_id", "deleted_at"}),
)


class Corridor(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "corridors"
    __table_args__ = (
        CheckConstraint("origin_area_id <> destination_area_id", name="ends"),
        Index(
            CORRIDOR_AREAS_UNIQUE,
            "origin_area_id",
            "destination_area_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_corridors_destination_area_id", "destination_area_id"),  # "area in use"
        Index("ix_corridors_created_at_id", "created_at", "id"),  # the list's order
    )

    origin_area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    destination_area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gize.areas.id", ondelete="RESTRICT")
    )
    # Loaded with the corridor where needed (repository); never lazily, which async can't do.
    origin_area: Mapped[Area] = relationship(foreign_keys=[origin_area_id], lazy="raise")
    destination_area: Mapped[Area] = relationship(foreign_keys=[destination_area_id], lazy="raise")
