"""
Subscription invoices, issued when a Stripe payment succeeds or a Super Admin
confirms a payment (see app/services/billing_service.py). Numbers are unique
and consecutive (SiteSettings.last_invoice_number). An issued row is never
deleted or renumbered: a mistake is corrected with a cancellation invoice
(cancels_id), and the PDF is stored as it was issued (§ 14, § 14b UStG).
"""

import uuid

from sqlalchemy import Column, String, DateTime, DECIMAL, Date, ForeignKey, Index, LargeBinary
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred
from sqlalchemy.sql import func

from app.database import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # RESTRICT: deleting an account must not silently delete invoices that have to be kept.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False)

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
    customer_street = Column(String(255), nullable=True)
    customer_zip_city = Column(String(255), nullable=True)
    customer_country = Column(String(2), nullable=True)
    customer_vat_id = Column(String(30), nullable=True)

    service_start = Column(Date, nullable=True)
    service_end = Column(Date, nullable=True)
    paid_on = Column(Date, nullable=True)
    payment_method = Column(String(20), nullable=True)

    # One invoice per Stripe payment, however often Stripe delivers the event.
    stripe_invoice_id = Column(String(255), nullable=True)
    cancels_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True)
    pdf = deferred(Column(LargeBinary, nullable=True))
    email_sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_invoices_stripe_invoice_id", "stripe_invoice_id", unique=True),
        Index("uq_invoices_cancels_id", "cancels_id", unique=True),
    )
