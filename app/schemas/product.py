import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    sku: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    image_url: str | None = None
    shelf_location: str | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None


class ProductUpdate(BaseModel):
    """All fields optional - PATCH semantics, only sent fields get changed."""
    sku: str | None = Field(None, min_length=1, max_length=100)
    title: str | None = Field(None, min_length=1, max_length=500)
    image_url: str | None = None
    shelf_location: str | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    active: bool | None = None


class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    title: str
    image_url: str | None
    shelf_location: str | None
    purchase_price: Decimal | None
    selling_price: Decimal | None
    active: bool

    # Computed, not stored - always derived fresh from the two price fields
    # above so it can never drift out of sync with them.
    profit_per_unit: Decimal | None = None
    margin_percent: Decimal | None = None

    class Config:
        from_attributes = True
