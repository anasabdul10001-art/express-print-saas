import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    price_monthly: Decimal | None = None
    currency: str = "EUR"
    order_limit: int | None = None
    printer_limit: int | None = None
    user_limit: int | None = None
    trial_days: int | None = None
    features: list[str] = Field(default_factory=list)
    is_active: bool = True
    is_recommended: bool = False
    display_order: int = 0
    stripe_price_id_monthly: str | None = None


class PlanUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price_monthly: Decimal | None = None
    currency: str | None = None
    order_limit: int | None = None
    printer_limit: int | None = None
    user_limit: int | None = None
    trial_days: int | None = None
    features: list[str] | None = None
    is_active: bool | None = None
    is_recommended: bool | None = None
    display_order: int | None = None
    stripe_price_id_monthly: str | None = None


class PlanOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price_monthly: Decimal | None
    currency: str
    order_limit: int | None
    printer_limit: int | None
    user_limit: int | None
    trial_days: int | None
    features: list[str]
    is_active: bool
    is_recommended: bool
    display_order: int
    stripe_price_id_monthly: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class SiteSettingsOut(BaseModel):
    logo_url: str | None
    logo_height: int | None
    company_legal_name: str | None
    company_address: str | None
    company_tax_id: str | None
    company_email: str | None

    class Config:
        from_attributes = True


class SiteSettingsUpdate(BaseModel):
    logo_height: int | None = Field(None, ge=12, le=200)
    company_legal_name: str | None = None
    company_address: str | None = None
    company_tax_id: str | None = None
    company_email: str | None = None


class PaymentMethodCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    details: str = Field(..., min_length=1)
    is_active: bool = True
    display_order: int = 0


class PaymentMethodUpdate(BaseModel):
    name: str | None = None
    details: str | None = None
    is_active: bool | None = None
    display_order: int | None = None


class PaymentMethodOut(BaseModel):
    id: uuid.UUID
    name: str
    details: str
    is_active: bool
    display_order: int
    created_at: datetime

    class Config:
        from_attributes = True


class TenantAdminOut(BaseModel):
    id: uuid.UUID
    company_name: str
    country_code: str
    status: str
    plan_id: uuid.UUID | None
    plan_name: str | None
    plan_price: Decimal | None
    plan_currency: str | None
    owner_email: str | None
    created_at: datetime


class ConfirmPaymentRequest(BaseModel):
    # Both optional - default to the tenant's current plan's name/price.
    # Set explicitly for a one-off or custom-priced charge.
    amount: Decimal | None = Field(None, gt=0)
    description: str | None = None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    invoice_number: str
    issue_date: date
    description: str
    net_amount: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    currency: str
    customer_name: str
    customer_email: EmailStr
    created_at: datetime

    class Config:
        from_attributes = True
