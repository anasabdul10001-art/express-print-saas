import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field
class ProductCreate(BaseModel):
    sku: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=500)
    image_url: str | None = None
    shelf_location: str | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    stock_quantity: int = 0
    weight_kg: Decimal | None = Field(None, gt=0)
    length_cm: int | None = Field(None, gt=0)
    width_cm: int | None = Field(None, gt=0)
    height_cm: int | None = Field(None, gt=0)
    # Dropshipping source monitoring (see app/services/source_monitor_service.py) -
    # the supplier's own product page. Optional; a product with none set is
    # simply never checked.
    source_url: str | None = Field(None, max_length=1000)
class ProductUpdate(BaseModel):
    """All fields optional - PATCH semantics, only sent fields get changed."""
    sku: str | None = Field(None, min_length=1, max_length=100)
    title: str | None = Field(None, min_length=1, max_length=500)
    image_url: str | None = None
    shelf_location: str | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    active: bool | None = None
    stock_quantity: int | None = None
    weight_kg: Decimal | None = Field(None, gt=0)
    length_cm: int | None = Field(None, gt=0)
    width_cm: int | None = Field(None, gt=0)
    height_cm: int | None = Field(None, gt=0)
    source_url: str | None = Field(None, max_length=1000)
class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    title: str
    image_url: str | None
    shelf_location: str | None
    purchase_price: Decimal | None
    selling_price: Decimal | None
    active: bool
    stock_quantity: int
    weight_kg: Decimal | None
    length_cm: int | None
    width_cm: int | None
    height_cm: int | None
    # Computed, not stored - always derived fresh from the two price fields
    # above so it can never drift out of sync with them.
    profit_per_unit: Decimal | None = None
    margin_percent: Decimal | None = None
    # Dropshipping source monitoring - see app/services/source_monitor_service.py.
    source_url: str | None = None
    source_price: Decimal | None = None
    last_checked_at: datetime | None = None
    last_check_status: str | None = None
    last_check_message: str | None = None
    class Config:
        from_attributes = True
class RestockCreate(BaseModel):
    """Adds stock through a dedicated endpoint (not a plain PATCH) so the
    addition is always logged as a StockMovement - a PATCH to stock_quantity
    overwrites the number with no history of why it changed."""
    quantity: int = Field(..., gt=0)
class StockMovementOut(BaseModel):
    id: uuid.UUID
    quantity_change: int
    reason: str
    reference: str | None
    created_at: datetime
    class Config:
        from_attributes = True