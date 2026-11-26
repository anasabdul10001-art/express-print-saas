"""What the seller reads off eBay's invoices for one VAT return period."""

import uuid

from sqlalchemy import Column, DECIMAL, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class VatPeriodInput(Base):
    __tablename__ = "vat_period_inputs"
    __table_args__ = (UniqueConstraint("tenant_id", "year", "period", name="uq_vat_period_inputs"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    year = Column(Integer, nullable=False)
    period = Column(String(3), nullable=False)

    ebay_fees_tax = Column(String(20))       # de_vat | eu_service | no_vat; empty = not decided
    ebay_amount = Column(DECIMAL(10, 2))     # de_vat: the VAT shown; eu_service: the net amount
    labels_vat = Column(DECIMAL(10, 2))      # VAT shown for shipping labels; empty = not entered
