"""Phone verifications (GZ-031): one row per code sent.

The code itself is never stored: `code_hash` is an HMAC of the verification's ID and the
code, keyed by VERIFIED_PHONE_TOKEN_KEY, so a copy of the table can't be turned back into
codes. A verification is usable until `expires_at` (10 minutes), for five attempts, once.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.contact import MOBILE_E164_LENGTH
from gize_api.core.models import Base, TimestampMixin

MAX_ATTEMPTS = 5


class PhoneVerification(TimestampMixin, Base):
    __tablename__ = "phone_verifications"
    __table_args__ = (
        CheckConstraint("phone ~ '^\\+251[79][0-9]{8}$'", name="phone"),
        CheckConstraint("code_hash ~ '^[0-9a-f]{64}$'", name="code_hash"),
        CheckConstraint(f"attempts BETWEEN 0 AND {MAX_ATTEMPTS}", name="attempts"),
        Index("ix_phone_verifications_created_at", "created_at"),
    )

    # Chosen by the service before the insert: the code's hash covers it.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(MOBILE_E164_LENGTH))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
