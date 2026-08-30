import uuid

from sqlalchemy import Column, String, DateTime, Integer, DECIMAL, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    external_order_ref = Column(String(100))  # e.g. eBay order ID, once that integration exists
    status = Column(String(20), nullable=False, default="new")

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('new','ready_to_print','printed','shipped','canceled')",
            name="ck_orders_status",
        ),
    )

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=True)

    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(DECIMAL(10, 2))

    # A shelf_location SNAPSHOT, captured at order-import time. This exists
    # as a fallback/audit trail (e.g. if a product is later deleted or
    # re-SKU'd, this order still shows where it was picked from). It is
    # deliberately NOT the primary source for what a picker sees on screen -
    # see the note in schemas/order.py: the dashboard/label always prefers
    # the PRODUCT's current shelf_location, since that reflects where the
    # item actually is right now. If stock gets relocated after the order
    # comes in but before it's picked, showing the live location prevents
    # sending someone to an empty shelf.
    shelf_location = Column(String(50))

    order = relationship("Order", back_populates="items")
    product = relationship("Product")
