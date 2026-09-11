import uuid
from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field
class OrderItemCreate(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(1, ge=1)
class OrderCreate(BaseModel):
    external_order_ref: str | None = None
    items: list[OrderItemCreate] = Field(..., min_length=1)
class OrderItemOut(BaseModel):
    id: uuid.UUID
    product_title: str
    quantity: int
    unit_price: Decimal | None
    # These two always come from the PRODUCT, not the order_item snapshot -
    # see the comment in models/order.py for why (live shelf location beats
    # a stale one). shelf_location falls back to the order_item's own
    # snapshot only if the linked product no longer exists.
    image_url: str | None
    shelf_location: str | None
    # Computed from unit_price and the purchase_price SNAPSHOT (not the
    # product's current cost) - see models/order.py for why the snapshot
    # is used here instead of a live value.
    profit: Decimal | None = None
    class Config:
        from_attributes = True
class OrderOut(BaseModel):
    id: uuid.UUID
    external_order_ref: str | None
    status: str
    items: list[OrderItemOut]
    # Sum of all items' profit. None if any item is missing cost data
    # (can't claim a total profit that's silently partial).
    total_profit: Decimal | None = None
    class Config:
        from_attributes = True
class OrderSummaryOut(BaseModel):
    orders_today: int
    profit_today: Decimal
    orders_month: int
    profit_month: Decimal


class ProfitHistoryPoint(BaseModel):
    date: date
    profit: Decimal