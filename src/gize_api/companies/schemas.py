"""Company request and response bodies."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from gize_api.companies.models import CompanyBilling
from gize_api.core.contact import EMAIL_MAX_LENGTH, MOBILE_E164_LENGTH, Email, EthiopianMobile
from gize_api.core.pagination import NameSortedParams, Page
from gize_api.core.text import NOTES_MAX_LENGTH, SHORT_TEXT_MAX_LENGTH, Notes, SearchText, ShortText
from gize_api.core.updates import PartialUpdate


class CompanyCreate(BaseModel):
    """A new company, active, billed `postpaid`. The phone may be typed any usual way
    (`0911 23 45 67`); it is stored `+251911234567`. Empty notes are no notes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ShortText
    contact_name: ShortText
    contact_phone: EthiopianMobile
    contact_email: Email
    notes: Notes | None = None


class CompanyUpdate(PartialUpdate):
    """Change any of these (leave out what stays). `is_active: false` deactivates the
    company; empty notes clear them."""

    name: ShortText | None = None
    contact_name: ShortText | None = None
    contact_phone: EthiopianMobile | None = None
    contact_email: Email | None = None
    notes: Notes | None = None
    is_active: StrictBool | None = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    contact_name: str = Field(max_length=SHORT_TEXT_MAX_LENGTH)
    contact_phone: str = Field(max_length=MOBILE_E164_LENGTH, description="E.164, +2519…/+2517…")
    contact_email: str = Field(max_length=EMAIL_MAX_LENGTH)
    billing: CompanyBilling
    notes: str | None = Field(max_length=NOTES_MAX_LENGTH)
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CompanyPage(Page[CompanyRead]):
    """A page of companies (a named schema: generated clients keep the name)."""


class CompanyListParams(NameSortedParams):
    """`q`: names starting with it, ignoring case; blank is no filter."""

    q: SearchText = None
    is_active: bool | None = None
