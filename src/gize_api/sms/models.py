"""SMS messages (GZ-030): one row per SMS the app tries to send, for de-duplication and the
outcome. The text is never stored (it may hold a code); the phone is, as on every record
that names a person.

`dedup_key` names the event (e.g. `otp:<verification id>`): a second send for the same
event finds the row and sends nothing (`uq_sms_messages_dedup_key`).
"""

from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.contact import MOBILE_E164_LENGTH
from gize_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

DEDUP_KEY_MAX_LENGTH = 100
PROVIDER_ID_MAX_LENGTH = 100


class SmsPurpose(StrEnum):
    PHONE_VERIFICATION = "phone_verification"  # GZ-031


class SmsStatus(StrEnum):
    SENDING = "sending"  # claimed; the send is under way (or the process died in it)
    SENT = "sent"
    FAILED = "failed"


def _string_enum[E: StrEnum](kind: type[E]) -> Enum:
    return Enum(
        kind,
        native_enum=False,  # VARCHAR + a named check: a new value is a plain migration
        create_constraint=False,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        length=30,
    )


class SmsMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sms_messages"
    __table_args__ = (
        CheckConstraint(
            f"dedup_key = btrim(dedup_key) AND dedup_key <> ''"
            f" AND length(dedup_key) <= {DEDUP_KEY_MAX_LENGTH}",
            name="dedup_key",
        ),
        CheckConstraint("purpose IN ('phone_verification')", name="purpose"),
        CheckConstraint("to_phone ~ '^\\+251[79][0-9]{8}$'", name="to_phone"),
        CheckConstraint(
            "status IN ('sending', 'sent', 'failed')"
            " AND (status = 'failed') = (error IS NOT NULL)"
            " AND (error IS NULL OR error IN ('unavailable', 'rejected', 'timeout', 'error'))",
            name="status",
        ),
        Index("uq_sms_messages_dedup_key", "dedup_key", unique=True),
        Index("ix_sms_messages_created_at", "created_at"),
    )

    dedup_key: Mapped[str] = mapped_column(String(DEDUP_KEY_MAX_LENGTH))
    purpose: Mapped[SmsPurpose] = mapped_column(_string_enum(SmsPurpose))
    to_phone: Mapped[str] = mapped_column(String(MOBILE_E164_LENGTH))
    status: Mapped[SmsStatus] = mapped_column(
        _string_enum(SmsStatus), server_default=SmsStatus.SENDING.value
    )
    # An SmsSendError kind, or `error` for anything else the sender raised.
    error: Mapped[str | None] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(PROVIDER_ID_MAX_LENGTH))
