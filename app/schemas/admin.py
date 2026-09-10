import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    price_monthly: Decimal | None = None
    currency: str = "EUR"
    order_limit: int | None = None
    printer_limit: int | None = None
    user_limit: int | None = None
    features: list[str] = Field(default_factory=list)
    is_active: bool = True
    is_recommended: bool = False
    display_order: int = 0


class PlanUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    price_monthly: Decimal | None = None
    currency: str | None = None
    order_limit: int | None = None
    printer_limit: int | None = None
    user_limit: int | None = None
    features: list[str] | None = None
    is_active: bool | None = None
    is_recommended: bool | None = None
    display_order: int | None = None


class PlanOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price_monthly: Decimal | None
    currency: str
    order_limit: int | None
    printer_limit: int | None
    user_limit: int | None
    features: list[str]
    is_active: bool
    is_recommended: bool
    display_order: int
    created_at: datetime

    class Config:
        from_attributes = True


class SiteSettingsOut(BaseModel):
    logo_url: str | None

    class Config:
        from_attributes = True
