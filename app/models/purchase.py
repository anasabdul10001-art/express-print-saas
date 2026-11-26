"""
Goods bought from a supplier: the supplier's invoice (with its file) and the
products on it. Booking a purchase updates each product's average unit cost.

Purchases are NOT expenses: goods already count in the profit through the product
costs (see routers/accounting.py), so adding them to expenses would count them twice.
"""

import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, DECIMAL, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred, relationship
from sqlalchemy.sql import func

from app.database import Base


class Purchase(Base):
    __tablename__ = "purchases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    supplier = Column(String(255), nullable=False)
    invoice_number = Column(String(100))
    invoice_date = Column(Date, nullable=False)
    # When the money left the account (what a cash-basis report will need).
    paid_date = Column(Date)
    # All amounts in EUR, as really paid.
    extra_costs = Column(DECIMAL(10, 2), nullable=False, default=0)  # shipping, customs, ... for the whole invoice
    total = Column(DECIMAL(10, 2), nullable=False)                   # goods + extra costs
    added_to_stock = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    file_name = Column(String(255), nullable=False)
    content_type = Column(String(50), nullable=False)
    file_data = deferred(Column(LargeBinary, nullable=False))  # only loaded for downloads

    tax_treatment = Column(String(20))  # for the VAT advance return; empty = not decided
    vat_amount = Column(DECIMAL(10, 2))  # VAT as shown on a German invoice

    cancelled_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    lines = relationship("PurchaseLine", back_populates="purchase", cascade="all, delete-orphan",
                         order_by="PurchaseLine.position")


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_id = Column(UUID(as_uuid=True), ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"))
    position = Column(Integer, nullable=False)
    title = Column(String(500), nullable=False)
    quantity = Column(Integer, nullable=False)
    line_total = Column(DECIMAL(10, 2), nullable=False)       # goods only, as on the invoice (in EUR)
    unit_extra = Column(DECIMAL(10, 4), nullable=False)       # share of the extra costs per piece

    # What the booking changed, so it can be shown and taken back.
    stock_before = Column(Integer, nullable=False)            # pieces the old average was weighted with
    old_purchase_price = Column(DECIMAL(10, 2))
    old_shipping_cost = Column(DECIMAL(10, 2))
    new_purchase_price = Column(DECIMAL(10, 2), nullable=False)
    new_shipping_cost = Column(DECIMAL(10, 2), nullable=False)

    purchase = relationship("Purchase", back_populates="lines")
