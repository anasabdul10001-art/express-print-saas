"""
Payment methods (e.g. bank transfer, PayPal), managed by the Super Admin
and shown to customers on the pricing page - informational content only,
same as Plan (see app/models/plan.py), not wired up to any real payment
processing yet.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    details = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)  # shown on the public pricing page
    display_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
