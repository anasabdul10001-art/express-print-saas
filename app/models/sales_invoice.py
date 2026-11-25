"""
Invoices a seller (tenant) issues to their own buyers. Not to be confused with
app/models/invoice.py, which are the platform's invoices to its tenants.

An issued row is never edited or deleted: `pdf` is the document as issued and `data`
a snapshot of what is printed on it, so later changes to the order, products or seller details can't rewrite
it. A mistake is corrected with a cancellation invoice (kind='storno').
"""

import uuid

from sqlalchemy import Column, Date, DateTime, DECIMAL, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class SellerProfile(Base):
    """The seller details printed on their invoices, plus the number counter."""

    __tablename__ = "seller_profiles"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    legal_name = Column(String(255))
    address = Column(String(1000))
    tax_number = Column(String(50))
    vat_id = Column(String(30))
    email = Column(String(255))
    number_prefix = Column(String(10), nullable=False, default="RE")
    # Last number handed out. Only ever counts up, so numbers stay unique.
    last_number = Column(Integer, nullable=False, default=0)


class SalesInvoice(Base):
    __tablename__ = "sales_invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_sales_invoices_tenant_number"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)

    number = Column(String(30), nullable=False)
    kind = Column(String(10), nullable=False, default="invoice")  # invoice / storno
    # For a storno: the invoice it cancels.
    cancels_id = Column(UUID(as_uuid=True), ForeignKey("sales_invoices.id"), nullable=True, unique=True)

    issue_date = Column(Date, nullable=False)
    gross_amount = Column(DECIMAL(10, 2), nullable=False)
    data = Column(Text, nullable=False)  # JSON snapshot, see services/sales_invoice_service.py
    # The document exactly as issued; downloads return these bytes, never a re-rendering.
    pdf = Column(LargeBinary, nullable=False)
    # The EN 16931 XML issued with it; null if the data wasn't enough for an e-invoice.
    xml = Column(LargeBinary, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
