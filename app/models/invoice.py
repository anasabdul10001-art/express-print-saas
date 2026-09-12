"""
Invoices, created when a Super Admin manually confirms a tenant's payment
(see app/routers/admin.py::confirm_payment). Numbers are sequential and
gapless per-year (SiteSettings.last_invoice_number), as German law (§14
UStG) requires - so a row here is never deleted or renumbered once issued.
"""

import uuid

from sqlalchemy import Column, DateTime, DECIMAL, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    invoice_number = Column(String(20), nullable=False, unique=True)
    issue_date = Column(Date, nullable=False)
    description = Column(String(255), nullable=False)

    net_amount = Column(DECIMAL(10, 2), nullable=False)
    vat_rate = Column(DECIMAL(5, 2), nullable=False)
    vat_amount = Column(DECIMAL(10, 2), nullable=False)
    gross_amount = Column(DECIMAL(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="EUR")

    # Snapshots, not live joins - an invoice must keep showing exactly what
    # the customer was billed as at the time, even if they rename their
    # company or change their email afterwards.
    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
