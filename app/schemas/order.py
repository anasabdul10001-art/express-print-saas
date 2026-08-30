import uuid
from decimal import Decimal

from pydantic import BaseModel


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

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    id: uuid.UUID
    external_order_ref: str | None
    status: str
    items: list[OrderItemOut]

    class Config:
        from_attributes = True
