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

from sqlalchemy import Column, DateTime, DECIMAL, Date, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred, relationship
from sqlalchemy.sql import func

from app.database import Base


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    description = Column(String(255), nullable=False)
    amount = Column(DECIMAL(10, 2), nullable=False)
    category = Column(String(50), nullable=True)
    expense_date = Column(Date, nullable=False)
    # Set for expenses imported from eBay (its transaction id), so a re-sync never duplicates them.
    external_ref = Column(String(100), nullable=True)  # the date the cost applies to, not when it was logged

    # Who was paid and the number on their receipt.
    supplier = Column(String(255), nullable=True)
    invoice_number = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    receipts = relationship("ExpenseReceipt", cascade="all, delete-orphan", lazy="selectin",
                            order_by="ExpenseReceipt.created_at")


class ExpenseReceipt(Base):
    __tablename__ = "expense_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    expense_id = Column(UUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False, index=True)

    file_name = Column(String(255), nullable=False)
    content_type = Column(String(50), nullable=False)
    file_data = deferred(Column(LargeBinary, nullable=False))  # only loaded for downloads

    created_at = Column(DateTime(timezone=True), server_default=func.now())
