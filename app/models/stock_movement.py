"""
Detailed, append-only log of every change to a product's stock_quantity.

Design choices:
    - This table is ADDITIVE to Product.stock_quantity, not a replacement.
      stock_quantity stays the fast "what's on hand right now" number used
      everywhere else (order creation checks, product list display).
      StockMovement is the "how did we get here" audit trail - each row is
      one delta, never edited or deleted, so the history is always
      trustworthy even if stock_quantity itself gets manually corrected.
    - reason is a plain string, not a foreign key, because the source of a
      movement varies (a manual order, an eBay-synced order, a manual
      restock) and forcing all of them through one reference column would
      either require nullable FKs to three different tables or a generic
      "reference_type/reference_id" pair - more complexity than a small
      table like this needs today. `reference` stores a human-readable
      pointer (e.g. an order's external_order_ref) when one exists.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)

    # Positive = stock added (restock, manual correction upward).
    # Negative = stock removed (order placed, manual correction downward).
    quantity_change = Column(Integer, nullable=False)

    # One of: "order", "ebay_sync", "restock", "manual_correction"
    reason = Column(String(30), nullable=False)

    # Optional human-readable pointer, e.g. an order's external_order_ref.
    # Not a foreign key on purpose - see module docstring.
    reference = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
