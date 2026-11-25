"""
One real money movement of an order, with the day it happened (from eBay's
Finances API). The order itself only keeps totals; the cash-basis report (EÜR)
needs to know WHEN each amount was paid or received.
"""

import uuid

from sqlalchemy import Column, Date, DateTime, DECIMAL, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class CashEvent(Base):
    __tablename__ = "cash_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "external_ref", "order_id", name="uq_cash_events_ref"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(20), nullable=False)          # sale_fee | refund | fee_credit | label
    amount = Column(DECIMAL(10, 2), nullable=False)    # positive = the normal direction of this kind
    booked_on = Column(Date, nullable=False)           # Europe/Berlin day of the transaction
    external_ref = Column(String(100), nullable=False)  # eBay transaction id

    created_at = Column(DateTime(timezone=True), server_default=func.now())
