"""A test-only model and table, shared by database tests (`widgets_table` in conftest)."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Widget(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A test-only table, created and dropped by the migrator around these tests."""

    __tablename__ = "test_widgets"

    label: Mapped[str] = mapped_column(String(50))
