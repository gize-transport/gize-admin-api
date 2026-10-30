"""Company accounts (GZ-029). A corporate route belongs to one company; the company is
billed afterwards for the service provided (postpaid, product brief).

The contact person's name, phone and email, and the notes, are personal data: the audit
log keeps only that they changed. Names are unique ignoring case among live companies.
"""

from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Enum, Index, String, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from gize_api.core.audit import AuditedEntity
from gize_api.core.contact import EMAIL_MAX_LENGTH, MOBILE_E164_LENGTH
from gize_api.core.models import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from gize_api.core.text import NOTES_MAX_LENGTH, SHORT_TEXT_MAX_LENGTH

COMPANY_NAME_UNIQUE = "uq_companies_name"

COMPANY_AUDIT = AuditedEntity(
    type="company",
    fields=(
        "name",
        "contact_name",
        "contact_phone",
        "contact_email",
        "billing",
        "notes",
        "is_active",
        "deleted_at",
    ),
    visible=frozenset({"name", "billing", "is_active", "deleted_at"}),
)


class CompanyBilling(StrEnum):
    POSTPAID = "postpaid"


class Company(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("name = btrim(name) AND name <> ''", name="name"),
        CheckConstraint(
            "contact_name = btrim(contact_name) AND contact_name <> ''", name="contact_name"
        ),
        CheckConstraint("contact_phone ~ '^\\+251[79][0-9]{8}$'", name="contact_phone"),
        CheckConstraint(
            "contact_email = lower(btrim(contact_email)) AND position('@' in contact_email) > 1",
            name="contact_email",
        ),
        CheckConstraint("billing IN ('postpaid')", name="billing"),
        CheckConstraint("notes IS NULL OR (notes = btrim(notes) AND notes <> '')", name="notes"),
        Index(
            COMPANY_NAME_UNIQUE,
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_companies_created_at_id", "created_at", "id"),  # the list's order
    )

    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    contact_name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX_LENGTH))
    contact_phone: Mapped[str] = mapped_column(String(MOBILE_E164_LENGTH))
    contact_email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH))
    billing: Mapped[CompanyBilling] = mapped_column(
        Enum(
            CompanyBilling,
            native_enum=False,  # VARCHAR + the named check: a new kind is a plain migration
            create_constraint=False,
            values_callable=lambda kinds: [kind.value for kind in kinds],
            validate_strings=True,
            length=20,
        ),
        default=CompanyBilling.POSTPAID,
        server_default=CompanyBilling.POSTPAID.value,
    )
    notes: Mapped[str | None] = mapped_column(String(NOTES_MAX_LENGTH))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
