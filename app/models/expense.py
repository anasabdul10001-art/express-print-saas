"""
A simple expense log: one row per cost the seller wants to track (labels,
shipping supplies, subscriptions, eBay fees paid manually, etc.).

Deliberately flat - no categories table, no recurring-expense logic. This
mirrors the project's stated approach elsewhere (see StockMovement,
OrderItem snapshots): start with the simplest structure that answers the
real question ("what did we spend, and when"), and only add structure
later if a genuine need for it shows up.
"""

import uuid

from sqlalchemy import Column, DateTime, DECIMAL, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    description = Column(String(255), nullable=False)
    amount = Column(DECIMAL(10, 2), nullable=False)
    expense_date = Column(Date, nullable=False)  # the date the cost applies to, not when it was logged

    created_at = Column(DateTime(timezone=True), server_default=func.now())
